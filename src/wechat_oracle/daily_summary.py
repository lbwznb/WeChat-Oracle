"""Idempotent previous-day summary scheduler and delivery outbox."""
from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from typing import Callable

from loguru import logger

from .config import settings
from .db import get_conn, transaction
from .llm import LLMClient
from .replier import Replier
from .time_ranges import DEFAULT_TZ, previous_natural_day


def split_message(text: str, max_chars: int) -> list[str]:
    """Split on paragraph/line boundaries, with a hard character fallback."""
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    remaining = text.strip()
    parts: list[str] = []
    while len(remaining) > max_chars:
        cut = max(remaining.rfind("\n", 0, max_chars + 1), remaining.rfind("。", 0, max_chars + 1))
        if cut < max_chars // 2:
            cut = max_chars
        else:
            cut += 1
        parts.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        parts.append(remaining)
    return parts


def resolve_daily_groups(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Resolve configured group display names/ids to unique archived groups."""
    if not settings.groups:
        return []
    placeholders = ",".join("?" for _ in settings.groups)
    rows = conn.execute(
        f"""
        SELECT COALESCE(ga.canonical_group_id, messages.group_id) AS canonical_id,
               COALESCE(MAX(NULLIF(messages.group_name, '')), COALESCE(ga.canonical_group_id, messages.group_id)) AS group_name
          FROM messages
          LEFT JOIN group_aliases ga ON ga.alias_id=messages.group_id
         WHERE messages.group_id IN ({placeholders}) OR messages.group_name IN ({placeholders})
         GROUP BY canonical_id
         ORDER BY canonical_id
        """,
        [*settings.groups, *settings.groups],
    ).fetchall()
    return [(row["canonical_id"], row["group_name"]) for row in rows]


def run_daily_group(
    conn: sqlite3.Connection,
    *,
    group_id: str,
    group_name: str,
    replier: Replier,
    llm: LLMClient,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Generate once and safely resume delivery of an already-ready summary."""
    from .dispatcher import fetch_candidates, summarize_chat_hierarchical

    period = previous_natural_day(now=now)
    started = time.time()
    with transaction(conn):
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO summary_runs
                (group_id, group_name, period_start, period_end, trigger_kind,
                 status, started_at)
            VALUES (?, ?, ?, ?, 'daily', 'running', ?)
            """,
            (group_id, group_name, period.start_t, period.end_t, started),
        )
    if not cur.rowcount:
        return _resume_ready_delivery(
            conn,
            group_id=group_id,
            group_name=group_name,
            period_start=period.start_t,
            period_end=period.end_t,
            period_label=period.label,
            replier=replier,
            sleep=sleep,
        )

    run_id = int(cur.lastrowid)
    try:
        candidates = fetch_candidates(
            conn, group_id=group_id, target=None, since_t=period.start_t,
            until_t=period.end_t, limit=None, bot_name=settings.bot_name,
        )
        if len(candidates) < settings.daily_summary_min_messages:
            _finish_run(
                conn, run_id, "skipped", len(candidates), None,
                f"fewer than {settings.daily_summary_min_messages} effective messages",
            )
            return "skipped"
        summary = summarize_chat_hierarchical(
            llm, settings.pi_model if settings.agent_backend == "pi" else settings.llm_model,
            candidates, f"{period.label} 每日群聊总结",
            settings.data_dir / "llm_debug.log",
        ).strip()
        if not summary:
            raise RuntimeError("summary model returned empty text")
        with transaction(conn):
            conn.execute(
                "UPDATE summary_runs SET status='ready', message_count=?, summary_text=?, result=? WHERE run_id=?",
                (len(candidates), summary, "generated", run_id),
            )
            conn.execute(
                "INSERT INTO delivery_outbox (summary_run_id, status, created_at, updated_at) VALUES (?, 'pending', ?, ?)",
                (run_id, time.time(), time.time()),
            )
    except Exception as exc:
        _finish_run(conn, run_id, "failed", 0, None, f"{type(exc).__name__}: {exc}")
        logger.exception("daily summary generation failed for {}", group_id)
        return "failed"

    parts = split_message(f"【{period.label} 群聊总结】\n{summary}", settings.daily_summary_chunk_chars)
    with transaction(conn):
        claimed = conn.execute(
            """
            UPDATE delivery_outbox
               SET status='sending', attempt_count=attempt_count+1, updated_at=?
             WHERE summary_run_id=? AND status='pending'
            """,
            (time.time(), run_id),
        )
    if not claimed.rowcount:
        return "duplicate"
    try:
        for index, part in enumerate(parts):
            replier.send(group_name, None, part)
            if index + 1 < len(parts):
                sleep(settings.daily_summary_send_delay_seconds)
    except Exception as exc:
        # UI automation errors may occur after WeChat accepted the keystroke.
        # Do not retry automatically: duplicate summaries are worse than a
        # locally inspectable unknown delivery state.
        with transaction(conn):
            changed = conn.execute(
                "UPDATE delivery_outbox SET status='unknown', last_error=?, updated_at=? "
                "WHERE summary_run_id=? AND status='sending'",
                (f"{type(exc).__name__}: {exc}", time.time(), run_id),
            )
            if changed.rowcount:
                conn.execute(
                    "UPDATE summary_runs SET status='unknown', result=?, finished_at=? "
                    "WHERE run_id=? AND status='ready'",
                    ("delivery outcome unknown", time.time(), run_id),
                )
        return "unknown"

    with transaction(conn):
        changed = conn.execute(
            "UPDATE delivery_outbox SET status='sent', updated_at=? "
            "WHERE summary_run_id=? AND status='sending'",
            (time.time(), run_id),
        )
        if changed.rowcount:
            conn.execute(
                "UPDATE summary_runs SET status='sent', result=?, finished_at=? "
                "WHERE run_id=? AND status='ready'",
                (f"sent {len(parts)} part(s)", time.time(), run_id),
            )
        else:
            return "unknown"
    return "sent"


def _resume_ready_delivery(
    conn: sqlite3.Connection,
    *,
    group_id: str,
    group_name: str,
    period_start: int,
    period_end: int,
    period_label: str,
    replier: Replier,
    sleep: Callable[[float], None],
) -> str:
    """Resume only delivery states that are provably safe to send."""
    run = conn.execute(
        """
        SELECT run_id, status, summary_text
          FROM summary_runs
         WHERE group_id=? AND period_start=? AND period_end=? AND trigger_kind='daily'
        """,
        (group_id, period_start, period_end),
    ).fetchone()
    if run is None or run["status"] != "ready" or not run["summary_text"]:
        return "duplicate"

    run_id = int(run["run_id"])
    now = time.time()
    with transaction(conn):
        outbox = conn.execute(
            "SELECT status FROM delivery_outbox WHERE summary_run_id=?",
            (run_id,),
        ).fetchone()
        if outbox is None:
            conn.execute(
                "INSERT INTO delivery_outbox (summary_run_id, status, created_at, updated_at) "
                "VALUES (?, 'pending', ?, ?)",
                (run_id, now, now),
            )
            outbox_status = "pending"
        else:
            outbox_status = outbox["status"]

        if outbox_status == "sending":
            conn.execute(
                """
                UPDATE delivery_outbox
                   SET status='unknown', last_error=?, updated_at=?
                 WHERE summary_run_id=? AND status='sending'
                """,
                ("interrupted while sending; delivery outcome unknown", now, run_id),
            )
            conn.execute(
                "UPDATE summary_runs SET status='unknown', result=?, finished_at=? WHERE run_id=? AND status='ready'",
                ("delivery outcome unknown after interrupted send", now, run_id),
            )
            return "unknown"

        if outbox_status == "unknown":
            conn.execute(
                "UPDATE summary_runs SET status='unknown', result=?, finished_at=? WHERE run_id=? AND status='ready'",
                ("delivery outcome unknown", now, run_id),
            )
            return "unknown"

        if outbox_status in {"sent", "failed"}:
            conn.execute(
                "UPDATE summary_runs SET status=?, result=?, finished_at=? WHERE run_id=? AND status='ready'",
                (outbox_status, f"recovered terminal {outbox_status} delivery", now, run_id),
            )
            return "duplicate"

        claimed = conn.execute(
            """
            UPDATE delivery_outbox
               SET status='sending', attempt_count=attempt_count+1, updated_at=?
             WHERE summary_run_id=? AND status='pending'
            """,
            (now, run_id),
        )
        if not claimed.rowcount:
            return "duplicate"

    parts = split_message(
        f"【{period_label} 群聊总结】\n{run['summary_text']}",
        settings.daily_summary_chunk_chars,
    )
    try:
        for index, part in enumerate(parts):
            replier.send(group_name, None, part)
            if index + 1 < len(parts):
                sleep(settings.daily_summary_send_delay_seconds)
    except Exception as exc:
        with transaction(conn):
            changed = conn.execute(
                "UPDATE delivery_outbox SET status='unknown', last_error=?, updated_at=? "
                "WHERE summary_run_id=? AND status='sending'",
                (f"{type(exc).__name__}: {exc}", time.time(), run_id),
            )
            if changed.rowcount:
                conn.execute(
                    "UPDATE summary_runs SET status='unknown', result=?, finished_at=? "
                    "WHERE run_id=? AND status='ready'",
                    ("delivery outcome unknown", time.time(), run_id),
                )
        return "unknown"

    with transaction(conn):
        changed = conn.execute(
            "UPDATE delivery_outbox SET status='sent', updated_at=? "
            "WHERE summary_run_id=? AND status='sending'",
            (time.time(), run_id),
        )
        if changed.rowcount:
            conn.execute(
                "UPDATE summary_runs SET status='sent', result=?, finished_at=? "
                "WHERE run_id=? AND status='ready'",
                (f"sent {len(parts)} part(s)", time.time(), run_id),
            )
        else:
            return "unknown"
    return "sent"


def _finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    message_count: int,
    summary_text: str | None,
    result: str,
) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE summary_runs SET status=?, message_count=?, summary_text=?, result=?, finished_at=? WHERE run_id=?",
            (status, message_count, summary_text, result, time.time(), run_id),
        )


class DailySummaryScheduler:
    """Non-blocking dispatcher hook; catches up only the latest completed day."""

    def __init__(self, *, replier: Replier, llm_factory: Callable[[], LLMClient]) -> None:
        self._replier = replier
        self._llm_factory = llm_factory
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="daily-summary")
        self._future: Future[None] | None = None
        self._lock = threading.Lock()

    def maybe_submit(self, now: datetime | None = None) -> bool:
        with self._lock:
            if self._future is not None and not self._future.done():
                return False
            self._future = self._executor.submit(self._run, now or datetime.now(DEFAULT_TZ))
            return True

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=False)

    def _run(self, now: datetime) -> None:
        with get_conn() as conn:
            groups = resolve_daily_groups(conn)
            if not groups:
                logger.warning("daily summary: none of WO_GROUPS resolved to archived group ids")
                return
            llm = self._llm_factory()
            for group_id, group_name in groups:
                result = run_daily_group(
                    conn, group_id=group_id, group_name=group_name,
                    replier=self._replier, llm=llm, now=now,
                )
                logger.info("daily summary: group={} period={} result={}", group_id, previous_natural_day(now=now).label, result)

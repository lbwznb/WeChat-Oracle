"""Crash-safe hourly and daily chat-summary scheduling and delivery."""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from typing import Callable

from loguru import logger

from .config import settings
from .db import get_conn, transaction
from .llm import LLMClient
from .replier import Replier
from .time_ranges import (
    SummaryPeriod,
    latest_mature_summary_periods,
    previous_natural_day,
)


SUMMARY_HEADERS = {
    "hourly": "#过去一小时话题",
    "daily": "#过去一天话题",
}


def split_message(text: str, max_chars: int) -> list[str]:
    """Split on paragraph/line boundaries, with a hard character fallback."""
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    remaining = text.strip()
    parts: list[str] = []
    while len(remaining) > max_chars:
        cut = max(
            remaining.rfind("\n", 0, max_chars + 1),
            remaining.rfind("。", 0, max_chars + 1),
        )
        if cut < max_chars // 2:
            cut = max_chars
        else:
            cut += 1
        parts.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        parts.append(remaining)
    return parts


def resolve_summary_groups(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Resolve configured names/ids to unique canonical archived groups."""
    if not settings.groups:
        return []
    placeholders = ",".join("?" for _ in settings.groups)
    rows = conn.execute(
        f"""
        SELECT COALESCE(ga.canonical_group_id, messages.group_id) AS canonical_id,
               COALESCE(MAX(NULLIF(messages.group_name, '')),
                        COALESCE(ga.canonical_group_id, messages.group_id)) AS group_name
          FROM messages
          LEFT JOIN group_aliases ga ON ga.alias_id=messages.group_id
         WHERE messages.group_id IN ({placeholders})
            OR messages.group_name IN ({placeholders})
         GROUP BY canonical_id
         ORDER BY canonical_id
        """,
        [*settings.groups, *settings.groups],
    ).fetchall()
    allowed_names = set(settings.reply_allowed_groups)
    resolved: list[tuple[str, str]] = []
    for row in rows:
        canonical_id = str(row["canonical_id"])
        group_name = str(row["group_name"])
        if settings.raw_wechat_enabled:
            authorization = conn.execute(
                """
                SELECT display_name
                  FROM raw_group_authorizations
                 WHERE account_fingerprint=? AND canonical_group_id=? AND enabled=1
                """,
                (settings.raw_wechat_account, canonical_id),
            ).fetchone()
            if authorization is None:
                continue
            group_name = str(authorization["display_name"])
        if group_name not in allowed_names:
            continue
        resolved.append((canonical_id, group_name))
    return resolved


resolve_daily_groups = resolve_summary_groups


def _delivery_authorized(
    conn: sqlite3.Connection,
    *,
    group_id: str,
    group_name: str,
) -> bool:
    """Recheck persisted schedule scope immediately before an automatic send."""
    if group_name not in settings.reply_allowed_groups:
        return False
    if group_id not in settings.groups and group_name not in settings.groups:
        return False
    if not settings.raw_wechat_enabled:
        return True
    row = conn.execute(
        """
        SELECT display_name
          FROM raw_group_authorizations
         WHERE account_fingerprint=? AND canonical_group_id=? AND enabled=1
        """,
        (settings.raw_wechat_account, group_id),
    ).fetchone()
    return row is not None and str(row["display_name"]) == group_name


def run_summary_group(
    conn: sqlite3.Connection,
    *,
    group_id: str,
    group_name: str,
    period: SummaryPeriod,
    min_messages: int,
    replier: Replier,
    llm: LLMClient,
    sleep: Callable[[float], None] = time.sleep,
    require_current_authorization: bool = False,
) -> str:
    """Generate one period once, then deliver or safely resume its outbox."""
    from .dispatcher import fetch_candidates, summarize_chat_hierarchical

    if require_current_authorization and not _delivery_authorized(
        conn, group_id=group_id, group_name=group_name
    ):
        return "unauthorized"
    claimed = _claim_generation(conn, group_id, group_name, period)
    if claimed is None:
        return _resume_period_delivery(
            conn,
            group_id=group_id,
            period=period,
            replier=replier,
            sleep=sleep,
            require_current_authorization=require_current_authorization,
        )
    run_id, lease_token = claimed

    try:
        candidates = fetch_candidates(
            conn,
            group_id=group_id,
            target=None,
            since_t=period.start_t,
            until_t=period.end_t,
            limit=None,
            bot_name=settings.bot_name,
        )
        if len(candidates) < min_messages:
            _finish_generation(
                conn,
                run_id,
                lease_token,
                status="skipped",
                message_count=len(candidates),
                summary_text=None,
                result=f"fewer than {min_messages} effective messages",
            )
            return "skipped"

        detail_request = (
            f"{period.label} 自动群聊总结。请按实际话题分段，尽量详细地写清人物观点、"
            "事情经过、结论、分歧和待办；每个话题可用一个贴切 emoji 开头。"
            "不要添加总标题，不要使用 @，不要编造原文没有的信息。"
        )
        summary = summarize_chat_hierarchical(
            llm,
            settings.llm_model,
            candidates,
            detail_request,
            settings.data_dir / "llm_debug.log",
        ).strip()
        if not summary:
            raise RuntimeError("summary model returned empty text")
        summary = summary.replace("@", "＠")
        if not _finish_generation(
            conn,
            run_id,
            lease_token,
            status="ready",
            message_count=len(candidates),
            summary_text=summary,
            result="generated",
        ):
            return "duplicate"
        with transaction(conn):
            conn.execute(
                "INSERT OR IGNORE INTO delivery_outbox "
                "(summary_run_id, status, created_at, updated_at) "
                "VALUES (?, 'pending', ?, ?)",
                (run_id, time.time(), time.time()),
            )
    except Exception as exc:
        _finish_generation(
            conn,
            run_id,
            lease_token,
            status="failed",
            message_count=0,
            summary_text=None,
            result=f"{type(exc).__name__}: {exc}",
        )
        logger.exception("{} summary generation failed for {}", period.kind, group_id)
        return "failed"

    return _deliver_run(
        conn,
        run_id=run_id,
        replier=replier,
        sleep=sleep,
        require_current_authorization=require_current_authorization,
    )


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
    """Compatibility wrapper for callers that request the previous day."""
    day = previous_natural_day(now=now, tz=settings.summary_tz)
    return run_summary_group(
        conn,
        group_id=group_id,
        group_name=group_name,
        period=SummaryPeriod("daily", day.start_t, day.end_t, day.label),
        min_messages=settings.daily_summary_min_messages,
        replier=replier,
        llm=llm,
        sleep=sleep,
    )


def _claim_generation(
    conn: sqlite3.Connection,
    group_id: str,
    group_name: str,
    period: SummaryPeriod,
) -> tuple[int, str] | None:
    now = time.time()
    token = uuid.uuid4().hex
    lease_until = now + settings.summary_generation_lease_seconds
    with transaction(conn):
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO summary_runs
                (group_id, group_name, period_start, period_end, trigger_kind,
                 status, started_at, generation_attempt_count, lease_token,
                 lease_until, updated_at)
            VALUES (?, ?, ?, ?, ?, 'running', ?, 1, ?, ?, ?)
            """,
            (
                group_id,
                group_name,
                period.start_t,
                period.end_t,
                period.kind,
                now,
                token,
                lease_until,
                now,
            ),
        )
        if cur.rowcount:
            return int(cur.lastrowid), token
        row = conn.execute(
            """
            SELECT run_id, status, lease_until
              FROM summary_runs
             WHERE group_id=? AND period_start=? AND period_end=? AND trigger_kind=?
            """,
            (group_id, period.start_t, period.end_t, period.kind),
        ).fetchone()
        if row is None or row["status"] != "running":
            return None
        current_lease = row["lease_until"]
        if current_lease is not None and float(current_lease) > now:
            return None
        changed = conn.execute(
            """
            UPDATE summary_runs
               SET group_name=?, started_at=?, finished_at=NULL, result='',
                   generation_attempt_count=generation_attempt_count+1,
                   lease_token=?, lease_until=?, updated_at=?
             WHERE run_id=? AND status='running'
               AND (lease_until IS NULL OR lease_until<=?)
            """,
            (group_name, now, token, lease_until, now, int(row["run_id"]), now),
        )
        return (int(row["run_id"]), token) if changed.rowcount else None


def _finish_generation(
    conn: sqlite3.Connection,
    run_id: int,
    lease_token: str,
    *,
    status: str,
    message_count: int,
    summary_text: str | None,
    result: str,
) -> bool:
    now = time.time()
    with transaction(conn):
        changed = conn.execute(
            """
            UPDATE summary_runs
               SET status=?, message_count=?, summary_text=?, result=?,
                   finished_at=?, lease_token=NULL, lease_until=NULL, updated_at=?
             WHERE run_id=? AND status='running' AND lease_token=?
            """,
            (status, message_count, summary_text, result, now, now, run_id, lease_token),
        )
    return bool(changed.rowcount)


def _resume_period_delivery(
    conn: sqlite3.Connection,
    *,
    group_id: str,
    period: SummaryPeriod,
    replier: Replier,
    sleep: Callable[[float], None],
    require_current_authorization: bool = False,
) -> str:
    row = conn.execute(
        """
        SELECT run_id, status
          FROM summary_runs
         WHERE group_id=? AND period_start=? AND period_end=? AND trigger_kind=?
        """,
        (group_id, period.start_t, period.end_t, period.kind),
    ).fetchone()
    if row is None:
        return "duplicate"
    if row["status"] == "ready":
        return _deliver_run(
            conn,
            run_id=int(row["run_id"]),
            replier=replier,
            sleep=sleep,
            require_current_authorization=require_current_authorization,
        )
    return "duplicate"


def _deliver_run(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    replier: Replier,
    sleep: Callable[[float], None],
    require_current_authorization: bool = False,
) -> str:
    run = conn.execute(
        "SELECT group_id, group_name, trigger_kind, status, summary_text "
        "FROM summary_runs WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if run is None or run["status"] != "ready" or not run["summary_text"]:
        return "duplicate"
    if require_current_authorization and not _delivery_authorized(
        conn,
        group_id=str(run["group_id"]),
        group_name=str(run["group_name"] or ""),
    ):
        return "unauthorized"
    now = time.time()
    with transaction(conn):
        outbox = conn.execute(
            "SELECT status, updated_at FROM delivery_outbox WHERE summary_run_id=?",
            (run_id,),
        ).fetchone()
        if outbox is None:
            conn.execute(
                "INSERT INTO delivery_outbox "
                "(summary_run_id, status, created_at, updated_at) VALUES (?, 'pending', ?, ?)",
                (run_id, now, now),
            )
            outbox_status = "pending"
            updated_at = now
        else:
            outbox_status = str(outbox["status"])
            updated_at = float(outbox["updated_at"])

        if outbox_status == "sending":
            if updated_at + settings.summary_sending_lease_seconds > now:
                return "in_progress"
            conn.execute(
                "UPDATE delivery_outbox SET status='unknown', last_error=?, updated_at=? "
                "WHERE summary_run_id=? AND status='sending'",
                ("sending lease expired; delivery outcome unknown", now, run_id),
            )
            conn.execute(
                "UPDATE summary_runs SET status='unknown', result=?, finished_at=?, updated_at=? "
                "WHERE run_id=? AND status='ready'",
                ("delivery outcome unknown after interrupted send", now, now, run_id),
            )
            return "unknown"
        if outbox_status == "unknown":
            conn.execute(
                "UPDATE summary_runs SET status='unknown', result=?, finished_at=?, updated_at=? "
                "WHERE run_id=? AND status='ready'",
                ("delivery outcome unknown", now, now, run_id),
            )
            return "unknown"
        if outbox_status in {"sent", "failed"}:
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

    header = SUMMARY_HEADERS.get(str(run["trigger_kind"]), "#群聊话题")
    body = f"{header}\n\n{run['summary_text']}"
    parts = split_message(body, settings.daily_summary_chunk_chars)
    try:
        for index, part in enumerate(parts):
            replier.send(str(run["group_name"] or ""), None, part)
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
                    "UPDATE summary_runs SET status='unknown', result=?, finished_at=?, updated_at=? "
                    "WHERE run_id=? AND status='ready'",
                    ("delivery outcome unknown", time.time(), time.time(), run_id),
                )
        return "unknown"

    with transaction(conn):
        changed = conn.execute(
            "UPDATE delivery_outbox SET status='sent', updated_at=? "
            "WHERE summary_run_id=? AND status='sending'",
            (time.time(), run_id),
        )
        if not changed.rowcount:
            return "unknown"
        conn.execute(
            "UPDATE summary_runs SET status='sent', result=?, finished_at=?, updated_at=? "
            "WHERE run_id=? AND status='ready'",
            (f"sent {len(parts)} part(s)", time.time(), time.time(), run_id),
        )
    return "sent"


def recover_pending_deliveries(
    conn: sqlite3.Connection,
    *,
    replier: Replier,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Resume every provably safe historical ready/pending delivery."""
    rows = conn.execute(
        """
        SELECT sr.run_id, sr.group_id, sr.group_name
          FROM summary_runs sr
          LEFT JOIN delivery_outbox d ON d.summary_run_id=sr.run_id
         WHERE sr.status='ready'
           AND (d.delivery_id IS NULL OR d.status IN ('pending', 'sending', 'unknown'))
         ORDER BY sr.period_end, sr.trigger_kind, sr.group_id
        """
    ).fetchall()
    handled = 0
    for row in rows:
        if not _delivery_authorized(
            conn,
            group_id=str(row["group_id"]),
            group_name=str(row["group_name"] or ""),
        ):
            continue
        result = _deliver_run(
            conn,
            run_id=int(row["run_id"]),
            replier=replier,
            sleep=sleep,
            require_current_authorization=True,
        )
        if result in {"sent", "unknown"}:
            handled += 1
    return handled


class SummaryScheduler:
    """One serial queue for recovery, hourly summaries, and daily summaries."""

    def __init__(self, *, replier: Replier, llm_factory: Callable[[], LLMClient]) -> None:
        self._replier = replier
        self._llm_factory = llm_factory
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="summary")
        self._future: Future[None] | None = None
        self._lock = threading.Lock()

    def maybe_submit(self, now: datetime | None = None) -> bool:
        with self._lock:
            if self._future is not None:
                if not self._future.done():
                    return False
                try:
                    self._future.result()
                except Exception:
                    logger.exception("automatic summary worker failed")
            current = now or datetime.now(settings.summary_tz)
            self._future = self._executor.submit(self._run, current)
            return True

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=False)

    def _run(self, now: datetime) -> None:
        periods = latest_mature_summary_periods(
            now=now,
            tz=settings.summary_tz,
            grace_seconds=settings.summary_sync_grace_seconds,
            hourly=settings.hourly_summary_enabled,
            daily=settings.daily_summary_enabled,
        )
        with get_conn() as conn:
            recover_pending_deliveries(conn, replier=self._replier)
            groups = resolve_summary_groups(conn)
            if not groups:
                logger.warning("automatic summary: configured groups are not in the local archive yet")
                return
            llm = self._llm_factory()
            for period in periods:
                min_messages = (
                    settings.hourly_summary_min_messages
                    if period.kind == "hourly"
                    else settings.daily_summary_min_messages
                )
                for group_id, group_name in groups:
                    result = run_summary_group(
                        conn,
                        group_id=group_id,
                        group_name=group_name,
                        period=period,
                        min_messages=min_messages,
                        replier=self._replier,
                        llm=llm,
                        require_current_authorization=True,
                    )
                    logger.info(
                        "automatic summary: kind={} group={} period={} result={}",
                        period.kind,
                        group_id,
                        period.label,
                        result,
                    )


DailySummaryScheduler = SummaryScheduler

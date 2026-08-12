"""Per-member, per-group knowledge with auditable LLM updates.

The member knowledge store deliberately lives beside (and never instead of)
the raw :mod:`messages` table.  ``messages`` remains the source of truth for
chat history; this module only stores derived profiles, claims, evidence, and
watermarks.  Every public function takes an explicit ``group_id`` and all
member keys are normalized through :func:`normalize_member_id`, which keeps a
missing sender in an ``__unknown__`` bucket *for that group only*.

The LLM boundary is intentionally small.  ``run_member_update`` accepts the
existing ``LLMClient`` shape, but also accepts tiny test doubles exposing
``complete_json(system, user)``.  A complete chunk is validated before any
derived row is written; failed or malformed chunks therefore leave the cursor
unchanged and can safely be retried later.
"""

from __future__ import annotations

import inspect
import json
import logging
import sqlite3
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .config import settings

logger = logging.getLogger(__name__)

UNKNOWN_MEMBER_ID = "__unknown__"
PROFILE_SECTIONS = (
    "identity",
    "interests",
    "skills",
    "communication_style",
    "habits",
    "relationships",
    "opinions",
    "sensitive_inferences",
    "recent_focus",
)
_SECTION_SET = frozenset(PROFILE_SECTIONS)
_CLAIM_BASES = frozenset(("self_reported", "observed", "inferred"))
_CLAIM_STATUSES = frozenset(("current", "superseded", "deleted"))


def _now() -> float:
    return time.time()


def normalize_member_id(sender_wxid: object) -> str:
    """Return a stable member id, mapping null/blank values to ``__unknown__``."""

    if sender_wxid is None:
        return UNKNOWN_MEMBER_ID
    value = str(sender_wxid).strip()
    return value or UNKNOWN_MEMBER_ID


def _raw_sender_is_unknown(sender_wxid: object) -> bool:
    return sender_wxid is None or not str(sender_wxid).strip()


def _json_load(value: object, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _profile_defaults() -> dict[str, Any]:
    return {section: None for section in PROFILE_SECTIONS}


def _row_dict(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    return dict(row)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the additive member tables for callers using an old connection.

    ``init_db`` executes the canonical ``schema.sql`` and calls the same DDL
    from :mod:`db`; keeping this tiny idempotent fallback here makes public
    APIs safe for tests and old integrations that hand us a bare connection.
    """

    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_messages_group_sender_msg
            ON messages(group_id, sender_wxid, msg_id);
        CREATE TABLE IF NOT EXISTS member_profiles (
            group_id TEXT NOT NULL,
            sender_wxid TEXT NOT NULL,
            display_name TEXT,
            profile_json TEXT NOT NULL DEFAULT '{}',
            summary_text TEXT NOT NULL DEFAULT '',
            locked_sections_json TEXT NOT NULL DEFAULT '[]',
            version INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            deleted_at REAL,
            PRIMARY KEY(group_id, sender_wxid)
        );
        CREATE INDEX IF NOT EXISTS idx_member_profiles_group
            ON member_profiles(group_id, updated_at);
        CREATE TABLE IF NOT EXISTS member_alias_history (
            group_id TEXT NOT NULL,
            sender_wxid TEXT NOT NULL,
            alias TEXT NOT NULL,
            first_seen_at REAL NOT NULL,
            last_seen_at REAL NOT NULL,
            seen_count INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(group_id, sender_wxid, alias)
        );
        CREATE INDEX IF NOT EXISTS idx_member_alias_history_lookup
            ON member_alias_history(group_id, alias);
        CREATE TABLE IF NOT EXISTS member_claims (
            claim_id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT NOT NULL,
            sender_wxid TEXT NOT NULL,
            section TEXT NOT NULL CHECK(section IN (
                'identity','interests','skills','communication_style',
                'habits','relationships','opinions','sensitive_inferences',
                'recent_focus'
            )),
            claim_text TEXT NOT NULL,
            basis TEXT NOT NULL CHECK(basis IN ('self_reported','observed','inferred')),
            confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
            sensitive INTEGER NOT NULL DEFAULT 0 CHECK(sensitive IN (0,1)),
            status TEXT NOT NULL DEFAULT 'current'
                CHECK(status IN ('current','superseded','deleted')),
            superseded_by INTEGER,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            FOREIGN KEY(group_id, sender_wxid)
                REFERENCES member_profiles(group_id, sender_wxid)
                ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_member_claims_member_status
            ON member_claims(group_id, sender_wxid, status, section);
        CREATE TABLE IF NOT EXISTS member_claim_evidence (
            evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id INTEGER NOT NULL,
            group_id TEXT NOT NULL,
            sender_wxid TEXT NOT NULL,
            msg_id INTEGER NOT NULL,
            created_at REAL NOT NULL,
            UNIQUE(claim_id, msg_id),
            FOREIGN KEY(claim_id) REFERENCES member_claims(claim_id)
                ON DELETE CASCADE,
            FOREIGN KEY(msg_id) REFERENCES messages(msg_id)
                ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_member_claim_evidence_msg
            ON member_claim_evidence(group_id, sender_wxid, msg_id);
        CREATE TABLE IF NOT EXISTS member_update_state (
            group_id TEXT NOT NULL,
            sender_wxid TEXT NOT NULL,
            cursor_msg_id INTEGER NOT NULL DEFAULT 0,
            cursor_t INTEGER,
            full_history_complete INTEGER NOT NULL DEFAULT 0 CHECK(full_history_complete IN (0,1)),
            last_status TEXT NOT NULL DEFAULT 'idle',
            last_error TEXT,
            last_run_id INTEGER,
            updated_at REAL NOT NULL,
            PRIMARY KEY(group_id, sender_wxid),
            FOREIGN KEY(group_id, sender_wxid)
                REFERENCES member_profiles(group_id, sender_wxid)
                ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_member_update_state_due
            ON member_update_state(group_id, updated_at);
        CREATE TABLE IF NOT EXISTS member_update_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT NOT NULL,
            sender_wxid TEXT NOT NULL,
            mode TEXT NOT NULL CHECK(mode IN ('full','incremental','manual')),
            status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','skipped')),
            cursor_before INTEGER,
            cursor_after INTEGER,
            chunk_count INTEGER NOT NULL DEFAULT 0,
            message_count INTEGER NOT NULL DEFAULT 0,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            error_text TEXT,
            started_at REAL NOT NULL,
            finished_at REAL,
            details_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_member_update_runs_member
            ON member_update_runs(group_id, sender_wxid, started_at);
        """
    )


def _sync_aliases(conn: sqlite3.Connection, group_id: str, sender_wxid: str) -> None:
    """Fold observed display names into durable alias history."""

    rows = conn.execute(
        """
        SELECT sender_display, MIN(t) AS first_t, MAX(t) AS last_t, COUNT(*) AS n
          FROM messages
         WHERE group_id=?
           AND (CASE WHEN sender_wxid IS NULL OR TRIM(sender_wxid)='' THEN ?
                     ELSE TRIM(sender_wxid) END)=?
           AND sender_display IS NOT NULL AND TRIM(sender_display)<>''
         GROUP BY sender_display
        """,
        (group_id, UNKNOWN_MEMBER_ID, sender_wxid),
    ).fetchall()
    now = _now()
    for row in rows:
        alias = str(row["sender_display"]).strip()
        first = float(row["first_t"] or now)
        last = float(row["last_t"] or now)
        count = int(row["n"] or 1)
        conn.execute(
            """
            INSERT INTO member_alias_history
                (group_id,sender_wxid,alias,first_seen_at,last_seen_at,seen_count)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(group_id,sender_wxid,alias) DO UPDATE SET
                first_seen_at=MIN(member_alias_history.first_seen_at, excluded.first_seen_at),
                last_seen_at=MAX(member_alias_history.last_seen_at, excluded.last_seen_at),
                seen_count=MAX(member_alias_history.seen_count, excluded.seen_count)
            """,
            (group_id, sender_wxid, alias, first, last, count),
        )


def _ensure_profile(
    conn: sqlite3.Connection,
    group_id: str,
    sender_wxid: str,
    *,
    display_name: str | None = None,
    now: float | None = None,
) -> sqlite3.Row:
    now = _now() if now is None else now
    row = conn.execute(
        "SELECT * FROM member_profiles WHERE group_id=? AND sender_wxid=?",
        (group_id, sender_wxid),
    ).fetchone()
    if row is None:
        if not display_name:
            observed = conn.execute(
                """
                SELECT sender_display FROM messages
                 WHERE group_id=?
                   AND (CASE WHEN sender_wxid IS NULL OR TRIM(sender_wxid)='' THEN ? ELSE TRIM(sender_wxid) END)=?
                   AND sender_display IS NOT NULL AND TRIM(sender_display)<>''
                 ORDER BY t DESC, msg_id DESC LIMIT 1
                """,
                (group_id, UNKNOWN_MEMBER_ID, sender_wxid),
            ).fetchone()
            if observed is not None:
                display_name = str(observed["sender_display"]).strip()
        conn.execute(
            """
            INSERT INTO member_profiles
                (group_id,sender_wxid,display_name,profile_json,summary_text,
                 locked_sections_json,version,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                group_id,
                sender_wxid,
                display_name,
                _json_dump(_profile_defaults()),
                "",
                "[]",
                1,
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM member_profiles WHERE group_id=? AND sender_wxid=?",
            (group_id, sender_wxid),
        ).fetchone()
    elif display_name and not str(row["display_name"] or "").strip():
        conn.execute(
            "UPDATE member_profiles SET display_name=?, updated_at=? WHERE group_id=? AND sender_wxid=?",
            (display_name, now, group_id, sender_wxid),
        )
        row = conn.execute(
            "SELECT * FROM member_profiles WHERE group_id=? AND sender_wxid=?",
            (group_id, sender_wxid),
        ).fetchone()
    assert row is not None
    return row


def _profile_output(row: sqlite3.Row, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    profile = _json_load(row["profile_json"], _profile_defaults())
    if not isinstance(profile, dict):
        profile = _profile_defaults()
    for section in PROFILE_SECTIONS:
        profile.setdefault(section, None)
    locked = _json_load(row["locked_sections_json"], [])
    if not isinstance(locked, list):
        locked = []
    result: dict[str, Any] = {
        "group_id": row["group_id"],
        "sender_wxid": row["sender_wxid"],
        "display_name": row["display_name"],
        "aliases": [],
        "alias_history": [],
        "profile": profile,
        "profile_json": profile,
        "summary_text": row["summary_text"] or "",
        "locked_sections": [s for s in locked if s in _SECTION_SET],
        "version": int(row["version"] or 1),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "deleted_at": row["deleted_at"],
        "message_count": 0,
    }
    # Keep both a namespaced ``profile`` object and direct section keys.  The
    # former is convenient for callers rendering the complete document; the
    # latter preserves the ergonomic shape used by early integrations.
    result.update(profile)
    if conn is not None:
        aliases = conn.execute(
            """
            SELECT alias, first_seen_at, last_seen_at, seen_count
              FROM member_alias_history
             WHERE group_id=? AND sender_wxid=?
             ORDER BY last_seen_at DESC, alias COLLATE NOCASE
            """,
            (row["group_id"], row["sender_wxid"]),
        ).fetchall()
        result["alias_history"] = [dict(a) for a in aliases]
        result["aliases"] = [str(a["alias"]) for a in aliases]
        claims = conn.execute(
            """
            SELECT c.*, COALESCE(
                (SELECT json_group_array(e.msg_id) FROM member_claim_evidence e
                  WHERE e.claim_id=c.claim_id), '[]') AS evidence_json
              FROM member_claims c
             WHERE c.group_id=? AND c.sender_wxid=?
             ORDER BY c.claim_id
            """,
            (row["group_id"], row["sender_wxid"]),
        ).fetchall()
        result["claims"] = []
        for claim in claims:
            item = dict(claim)
            item["sensitive"] = bool(item.get("sensitive"))
            item["evidence"] = _json_load(item.pop("evidence_json", "[]"), [])
            item["evidence_ids"] = list(item["evidence"])
            result["claims"].append(item)
        state = conn.execute(
            "SELECT * FROM member_update_state WHERE group_id=? AND sender_wxid=?",
            (row["group_id"], row["sender_wxid"]),
        ).fetchone()
        result["state"] = dict(state) if state else None
        count = conn.execute(
            """
            SELECT COUNT(*) FROM messages
             WHERE group_id=?
               AND (CASE WHEN sender_wxid IS NULL OR TRIM(sender_wxid)='' THEN ? ELSE TRIM(sender_wxid) END)=?
            """,
            (row["group_id"], UNKNOWN_MEMBER_ID, row["sender_wxid"]),
        ).fetchone()
        result["message_count"] = int(count[0] or 0) if count else 0
    return result


def list_member_profiles(conn: sqlite3.Connection, group_id: str) -> list[dict[str, Any]]:
    """List non-deleted profiles belonging to exactly ``group_id``."""

    _ensure_schema(conn)
    # A member can be active in the archive before the first LLM bootstrap.
    # Materialize a minimal derived row for each observed identity so UIs do
    # not hide participants merely because their profile is still empty.
    observed = conn.execute(
        """
        SELECT CASE WHEN sender_wxid IS NULL OR TRIM(sender_wxid)='' THEN ? ELSE TRIM(sender_wxid) END AS member,
               MAX(t) AS last_t,
               COUNT(*) AS message_count,
               (SELECT m2.sender_display FROM messages m2
                 WHERE m2.group_id=?
                   AND (CASE WHEN m2.sender_wxid IS NULL OR TRIM(m2.sender_wxid)='' THEN ? ELSE TRIM(m2.sender_wxid) END)=
                       (CASE WHEN messages.sender_wxid IS NULL OR TRIM(messages.sender_wxid)='' THEN ? ELSE TRIM(messages.sender_wxid) END)
                   AND m2.sender_display IS NOT NULL AND TRIM(m2.sender_display)<>''
                 ORDER BY m2.t DESC, m2.msg_id DESC LIMIT 1) AS latest_display
          FROM messages
         WHERE group_id=?
         GROUP BY member
        """,
        (UNKNOWN_MEMBER_ID, group_id, UNKNOWN_MEMBER_ID, UNKNOWN_MEMBER_ID, group_id),
    ).fetchall()
    with conn:
        for item in observed:
            existing = conn.execute(
                "SELECT deleted_at FROM member_profiles WHERE group_id=? AND sender_wxid=?",
                (group_id, item["member"]),
            ).fetchone()
            if existing is not None and existing["deleted_at"] is not None:
                continue
            _ensure_profile(
                conn,
                group_id,
                item["member"],
                display_name=(str(item["latest_display"]).strip() if item["latest_display"] else None),
            )
    rows = conn.execute(
        "SELECT * FROM member_profiles WHERE group_id=? AND deleted_at IS NULL ORDER BY COALESCE(display_name,sender_wxid) COLLATE NOCASE",
        (group_id,),
    ).fetchall()
    for row in rows:
        _sync_aliases(conn, group_id, row["sender_wxid"])
    by_member = {str(item["member"]): int(item["message_count"] or 0) for item in observed}
    output = [_profile_output(row, conn) for row in rows]
    for item in output:
        item["message_count"] = by_member.get(str(item["sender_wxid"]), item.get("message_count", 0))
    return output


def get_member_profile(
    conn: sqlite3.Connection, group_id: str, sender_wxid: object
) -> dict[str, Any] | None:
    """Fetch one profile, never falling back to another group or member."""

    _ensure_schema(conn)
    member = normalize_member_id(sender_wxid)
    row = conn.execute(
        "SELECT * FROM member_profiles WHERE group_id=? AND sender_wxid=? AND deleted_at IS NULL",
        (group_id, member),
    ).fetchone()
    if row is None:
        return None
    _sync_aliases(conn, group_id, member)
    return _profile_output(row, conn)


def search_member_profiles(
    conn: sqlite3.Connection, group_id: str, query: str, limit: int = 10
) -> list[dict[str, Any]]:
    """Case-insensitive search over names, aliases, summaries, and claims."""

    _ensure_schema(conn)
    needle = str(query or "").strip().casefold()
    limit = max(0, int(limit))
    profiles = list_member_profiles(conn, group_id)
    if not needle:
        return profiles[:limit]
    result: list[dict[str, Any]] = []
    for profile in profiles:
        haystacks = [
            str(profile.get("display_name") or ""),
            str(profile.get("sender_wxid") or ""),
            str(profile.get("summary_text") or ""),
            json.dumps(profile.get("profile"), ensure_ascii=False),
        ]
        haystacks.extend(str(a) for a in profile.get("aliases", []))
        haystacks.extend(str(c.get("claim_text") or "") for c in profile.get("claims", []))
        if any(needle in text.casefold() for text in haystacks):
            result.append(profile)
            if len(result) >= limit:
                break
    return result


def list_member_messages(
    conn: sqlite3.Connection,
    group_id: str,
    sender_wxid: object,
    limit: int = 100,
    before_msg_id: int | None = None,
) -> list[dict[str, Any]]:
    """Return a member's raw messages newest-first, scoped by group.

    ``before_msg_id`` is an exclusive pagination boundary.  The unknown bucket
    matches only null/blank raw sender ids; a real wxid can never leak into it.
    """

    _ensure_schema(conn)
    member = normalize_member_id(sender_wxid)
    clauses = [
        "group_id=?",
        "(CASE WHEN sender_wxid IS NULL OR TRIM(sender_wxid)='' THEN ? ELSE TRIM(sender_wxid) END)=?",
    ]
    params: list[Any] = [group_id, UNKNOWN_MEMBER_ID, member]
    if before_msg_id is not None:
        clauses.append("msg_id < ?")
        params.append(int(before_msg_id))
    params.append(max(0, int(limit)))
    rows = conn.execute(
        "SELECT * FROM messages WHERE " + " AND ".join(clauses) + " ORDER BY msg_id DESC LIMIT ?",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def _message_render(row: Mapping[str, Any]) -> str:
    body = str(row.get("content_text") or "").strip()
    transcript = str(row.get("transcript") or "").strip()
    quote = str(row.get("quote_text") or "").strip()
    parts = [body] if body else []
    if transcript and transcript not in parts:
        parts.append(f"[transcript] {transcript}")
    if quote:
        parts.append(f"[quote] {quote}")
    if not parts:
        parts.append(f"[{row.get('type') or 'message'}]")
    return " ".join(parts)


def build_active_member_context(
    conn: sqlite3.Connection,
    group_id: str,
    start_t: int,
    end_t: int,
    max_chars: int = 20_000,
) -> str:
    """Render compact background profiles for participants active in a time range."""

    _ensure_schema(conn)
    rows = conn.execute(
        """
        SELECT CASE WHEN sender_wxid IS NULL OR TRIM(sender_wxid)='' THEN ? ELSE TRIM(sender_wxid) END AS member,
               COUNT(*) AS message_count,
               MAX(t) AS last_t,
               (SELECT m2.sender_display FROM messages m2
                 WHERE m2.group_id=? AND m2.t>=? AND m2.t<?
                   AND (CASE WHEN m2.sender_wxid IS NULL OR TRIM(m2.sender_wxid)='' THEN ? ELSE TRIM(m2.sender_wxid) END)=
                       (CASE WHEN messages.sender_wxid IS NULL OR TRIM(messages.sender_wxid)='' THEN ? ELSE TRIM(messages.sender_wxid) END)
                   AND m2.sender_display IS NOT NULL AND TRIM(m2.sender_display)<>''
                 ORDER BY m2.t DESC, m2.msg_id DESC LIMIT 1) AS latest_display
          FROM messages
         WHERE group_id=? AND t>=? AND t<?
         GROUP BY member
         ORDER BY last_t, member
        """,
        (UNKNOWN_MEMBER_ID, group_id, int(start_t), int(end_t), UNKNOWN_MEMBER_ID, UNKNOWN_MEMBER_ID, group_id, int(start_t), int(end_t)),
    ).fetchall()
    if not rows:
        return ""
    max_chars = max(0, int(max_chars))
    chunks: list[str] = []
    for row in rows:
        member = str(row["member"])
        count = int(row["message_count"] or 0)
        # Unknown senders are intentionally represented only as an aggregate;
        # no profile data is personalized to a member whose identity is absent.
        if member == UNKNOWN_MEMBER_ID:
            chunks.append(f"[unknown participants] {count} message(s)")
            continue
        profile = get_member_profile(conn, group_id, member)
        label = (profile or {}).get("display_name") or row["latest_display"] or member
        summary = (profile or {}).get("summary_text") or ""
        if not summary and profile:
            populated = {
                key: value
                for key, value in (profile.get("profile") or {}).items()
                if value not in (None, "", [], {}) and key != "sensitive_inferences"
            }
            summary = json.dumps(populated, ensure_ascii=False) if populated else ""
        if summary:
            chunks.append(f"[{label} ({member})] {summary} ({count} message(s))")
        else:
            chunks.append(f"[{label} ({member})] ({count} message(s))")
    # Enforce the bound on the complete rendered context, including separators.
    text = "\n".join(chunks)
    return text[:max_chars]


def update_member_profile_section(
    conn: sqlite3.Connection,
    group_id: str,
    sender_wxid: object,
    section: str,
    content: Any,
    locked: bool | None = None,
) -> dict[str, Any]:
    """Apply a direct profile edit, optionally changing its lock state."""

    _ensure_schema(conn)
    if section not in _SECTION_SET:
        raise ValueError(f"unknown profile section: {section!r}")
    member = normalize_member_id(sender_wxid)
    now = _now()
    with conn:
        row = _ensure_profile(conn, group_id, member, now=now)
        profile = _json_load(row["profile_json"], _profile_defaults())
        if not isinstance(profile, dict):
            profile = _profile_defaults()
        profile.update({key: profile.get(key) for key in PROFILE_SECTIONS if key not in profile})
        profile[section] = content
        locked_sections = _json_load(row["locked_sections_json"], [])
        if not isinstance(locked_sections, list):
            locked_sections = []
        locked_set = {item for item in locked_sections if item in _SECTION_SET}
        if locked is True:
            locked_set.add(section)
        elif locked is False:
            locked_set.discard(section)
        conn.execute(
            """
            UPDATE member_profiles
               SET profile_json=?, locked_sections_json=?, version=version+1,
                   updated_at=?, deleted_at=NULL
             WHERE group_id=? AND sender_wxid=?
            """,
            (_json_dump(profile), _json_dump(sorted(locked_set)), now, group_id, member),
        )
        _sync_aliases(conn, group_id, member)
        row = conn.execute(
            "SELECT * FROM member_profiles WHERE group_id=? AND sender_wxid=?",
            (group_id, member),
        ).fetchone()
    assert row is not None
    return _profile_output(row, conn)


def _mark_claims_deleted(conn: sqlite3.Connection, group_id: str, member: str, now: float) -> None:
    conn.execute(
        "UPDATE member_claims SET status='deleted', updated_at=? WHERE group_id=? AND sender_wxid=? AND status='current'",
        (now, group_id, member),
    )


def delete_member_profile(
    conn: sqlite3.Connection,
    group_id: str,
    sender_wxid: object,
    keep_messages: bool = True,
) -> bool:
    """Delete derived profile data while retaining audit rows and, by default, messages."""

    _ensure_schema(conn)
    member = normalize_member_id(sender_wxid)
    now = _now()
    with conn:
        row = conn.execute(
            "SELECT 1 FROM member_profiles WHERE group_id=? AND sender_wxid=?",
            (group_id, member),
        ).fetchone()
        if row is None:
            return False
        _mark_claims_deleted(conn, group_id, member, now)
        conn.execute(
            "UPDATE member_profiles SET deleted_at=?, updated_at=?, version=version+1 WHERE group_id=? AND sender_wxid=?",
            (now, now, group_id, member),
        )
        conn.execute(
            "UPDATE member_update_state SET last_status='deleted', updated_at=? WHERE group_id=? AND sender_wxid=?",
            (now, group_id, member),
        )
        if not keep_messages:
            # Explicit opt-in only.  Evidence rows cascade from messages, but
            # claims themselves remain as deleted audit records.
            conn.execute(
                "DELETE FROM messages WHERE group_id=? AND (CASE WHEN sender_wxid IS NULL OR TRIM(sender_wxid)='' THEN ? ELSE TRIM(sender_wxid) END)=?",
                (group_id, UNKNOWN_MEMBER_ID, member),
            )
    return True


def reset_member_profile(
    conn: sqlite3.Connection, group_id: str, sender_wxid: object
) -> dict[str, Any]:
    """Clear derived content and rewind the per-member cursor, preserving history."""

    _ensure_schema(conn)
    member = normalize_member_id(sender_wxid)
    now = _now()
    with conn:
        row = _ensure_profile(conn, group_id, member, now=now)
        _mark_claims_deleted(conn, group_id, member, now)
        conn.execute(
            """
            UPDATE member_profiles
               SET profile_json=?, summary_text='', locked_sections_json='[]',
                   version=version+1, updated_at=?, deleted_at=NULL
             WHERE group_id=? AND sender_wxid=?
            """,
            (_json_dump(_profile_defaults()), now, group_id, member),
        )
        conn.execute(
            """
            INSERT INTO member_update_state
                (group_id,sender_wxid,cursor_msg_id,full_history_complete,last_status,updated_at)
            VALUES(?,?,0,0,'idle',?)
            ON CONFLICT(group_id,sender_wxid) DO UPDATE SET
                cursor_msg_id=0,cursor_t=NULL,full_history_complete=0,
                last_status='idle',last_error=NULL,last_run_id=NULL,updated_at=excluded.updated_at
            """,
            (group_id, member, now),
        )
        row = conn.execute(
            "SELECT * FROM member_profiles WHERE group_id=? AND sender_wxid=?",
            (group_id, member),
        ).fetchone()
    assert row is not None
    return _profile_output(row, conn)


class _ValidationError(ValueError):
    pass


def _safe_error(exc: BaseException) -> str:
    """Bounded diagnostic that never includes prompt/model/chat text."""

    return f"{exc.__class__.__name__}: member update failed"


def _coerce_json_response(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        obj = dict(raw)
    else:
        if hasattr(raw, "content") and not isinstance(raw, (str, bytes)):
            raw = getattr(raw, "content")
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if not isinstance(raw, str) or not raw.strip():
            raise _ValidationError("LLM output must be a JSON object")
        try:
            obj = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise _ValidationError("LLM output is not valid JSON") from exc
    if not isinstance(obj, dict):
        raise _ValidationError("LLM output must be a JSON object")
    return obj


def _as_int(value: Any, *, label: str) -> int:
    if isinstance(value, bool):
        raise _ValidationError(f"{label} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise _ValidationError(f"{label} must be an integer") from exc
    if str(value).strip() != str(number) and not isinstance(value, int):
        raise _ValidationError(f"{label} must be an integer")
    return number


def _validate_llm_output(
    raw: Any,
    supplied: Sequence[Mapping[str, Any]],
    group_id: str,
    member: str,
    conn: sqlite3.Connection,
) -> dict[str, Any]:
    """Parse and validate one model result before touching the database."""

    obj = _coerce_json_response(raw)
    allowed = {
        "profile",
        "profile_json",
        "sections",
        "updates",
        "summary_text",
        "claims",
        "supersedes",
        "deletes",
    }
    unknown = set(obj) - allowed - _SECTION_SET
    if unknown:
        raise _ValidationError(f"unknown LLM output keys: {sorted(unknown)}")

    profile_obj: dict[str, Any] = {}
    for key in ("profile", "profile_json", "sections", "updates"):
        if key in obj:
            value = obj[key]
            if not isinstance(value, Mapping):
                raise _ValidationError(f"{key} must be an object")
            for section, content in value.items():
                if section not in _SECTION_SET:
                    raise _ValidationError(f"unknown profile section: {section!r}")
                # Round-trip check catches unserializable test doubles and
                # keeps stored JSON deterministic.
                try:
                    json.dumps(content, ensure_ascii=False)
                except (TypeError, ValueError) as exc:
                    raise _ValidationError(f"profile section {section!r} is not JSON-serializable") from exc
                profile_obj[section] = content
    for section in _SECTION_SET:
        if section in obj:
            try:
                json.dumps(obj[section], ensure_ascii=False)
            except (TypeError, ValueError) as exc:
                raise _ValidationError(f"profile section {section!r} is not JSON-serializable") from exc
            profile_obj[section] = obj[section]
    summary = obj.get("summary_text", None)
    if summary is not None and not isinstance(summary, str):
        raise _ValidationError("summary_text must be a string")

    supplied_by_id: dict[int, Mapping[str, Any]] = {}
    for row in supplied:
        supplied_by_id[int(row["msg_id"])] = row
    supplied_ids = set(supplied_by_id)
    claims_raw = obj.get("claims", [])
    if claims_raw is None:
        claims_raw = []
    if not isinstance(claims_raw, list):
        raise _ValidationError("claims must be an array")
    claims: list[dict[str, Any]] = []
    supersede_ids: list[int] = []
    seen_supersedes: set[int] = set()
    for raw_claim in claims_raw:
        if not isinstance(raw_claim, Mapping):
            raise _ValidationError("each claim must be an object")
        claim = dict(raw_claim)
        allowed_claim = {
            "section", "claim", "claim_text", "text", "content", "basis",
            "confidence", "sensitive", "evidence", "evidence_ids",
            "supersedes", "status",
        }
        if set(claim) - allowed_claim:
            raise _ValidationError("unknown claim keys")
        section = claim.get("section")
        if section not in _SECTION_SET:
            raise _ValidationError("claim section is invalid")
        text = claim.get("claim_text", claim.get("claim", claim.get("text", claim.get("content"))))
        if not isinstance(text, str) or not text.strip():
            raise _ValidationError("claim text is required")
        basis = claim.get("basis")
        if basis not in _CLAIM_BASES:
            raise _ValidationError("claim basis is invalid")
        confidence = claim.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise _ValidationError("claim confidence must be a number")
        if confidence < 0 or confidence > 1:
            raise _ValidationError("claim confidence must be between 0 and 1")
        sensitive = claim.get("sensitive", False)
        if not isinstance(sensitive, bool):
            raise _ValidationError("claim sensitive must be boolean")
        status = claim.get("status", "current")
        if status != "current":
            raise _ValidationError("new claims must have current status")
        evid = claim.get("evidence_ids", claim.get("evidence", []))
        if evid is None:
            evid = []
        if not isinstance(evid, list):
            raise _ValidationError("claim evidence must be an array")
        if not evid:
            raise _ValidationError("each new claim requires evidence from this update")
        evidence_ids: list[int] = []
        for evidence_id in evid:
            msg_id = _as_int(evidence_id, label="evidence id")
            if msg_id not in supplied_ids:
                raise _ValidationError("evidence id was not supplied in this update")
            msg = supplied_by_id[msg_id]
            if str(msg.get("group_id")) != str(group_id):
                raise _ValidationError("evidence crosses groups")
            raw_sender = msg.get("sender_wxid")
            if member == UNKNOWN_MEMBER_ID:
                if not _raw_sender_is_unknown(raw_sender):
                    raise _ValidationError("unknown member evidence must have blank sender")
            elif normalize_member_id(raw_sender) != member:
                raise _ValidationError("evidence crosses members")
            if msg_id not in evidence_ids:
                evidence_ids.append(msg_id)
        supersedes = claim.get("supersedes", [])
        if supersedes is None:
            supersedes = []
        if not isinstance(supersedes, list):
            raise _ValidationError("claim supersedes must be an array")
        ids: list[int] = []
        for claim_id in supersedes:
            parsed = _as_int(claim_id, label="superseded claim id")
            if parsed not in ids:
                ids.append(parsed)
        claims.append({
            "section": section,
            "claim_text": text.strip(),
            "basis": basis,
            "confidence": float(confidence),
            "sensitive": sensitive,
            "status": "current",
            "evidence_ids": evidence_ids,
            "supersedes": ids,
        })
        for claim_id in ids:
            if claim_id not in seen_supersedes:
                supersede_ids.append(claim_id)
                seen_supersedes.add(claim_id)
    for field in ("supersedes", "deletes"):
        if field in obj:
            values = obj[field]
            if not isinstance(values, list):
                raise _ValidationError(f"{field} must be an array")
            for claim_id in values:
                parsed = _as_int(claim_id, label=f"{field} claim id")
                if parsed not in seen_supersedes:
                    supersede_ids.append(parsed)
                    seen_supersedes.add(parsed)
    if supersede_ids:
        placeholders = ",".join("?" * len(supersede_ids))
        existing = conn.execute(
            f"SELECT claim_id FROM member_claims WHERE group_id=? AND sender_wxid=? AND status='current' AND claim_id IN ({placeholders})",
            [group_id, member, *supersede_ids],
        ).fetchall()
        if {int(r["claim_id"]) for r in existing} != set(supersede_ids):
            raise _ValidationError("supersedes may reference only current claims for this member")
    profile_row = conn.execute(
        "SELECT locked_sections_json FROM member_profiles WHERE group_id=? AND sender_wxid=?",
        (group_id, member),
    ).fetchone()
    locked_sections = set()
    if profile_row is not None:
        raw_locked = _json_load(profile_row["locked_sections_json"], [])
        if isinstance(raw_locked, list):
            locked_sections = {str(item) for item in raw_locked if item in _SECTION_SET}
    written_sections = set(profile_obj) - locked_sections
    evidenced_sections = {str(claim["section"]) for claim in claims}
    missing_evidence = written_sections - evidenced_sections
    if missing_evidence:
        raise _ValidationError(
            "each updated profile section requires an evidence-linked claim"
        )
    return {
        "profile": profile_obj,
        "summary_text": summary,
        "claims": claims,
        "supersedes": supersede_ids,
    }


def _call_complete_json(llm: Any, model: str, system: str, user: str) -> Any:
    method = getattr(llm, "complete_json", None)
    if method is None:
        raise TypeError("llm does not provide complete_json")
    # Existing LLMClient is keyword-only with a required model.  Tiny fakes in
    # integrations often expose only ``(system, user)``; inspect first so an
    # API exception is never accidentally retried through a second request.
    try:
        signature = inspect.signature(method)
        names = set(signature.parameters)
    except (TypeError, ValueError):
        names = set()
    if "model" in names:
        return method(model=model, system=system, user=user, temperature=0.0)
    if "system" in names and "user" in names:
        parameters = signature.parameters
        if all(
            parameters[name].kind is not inspect.Parameter.POSITIONAL_ONLY
            for name in ("system", "user")
        ):
            return method(system=system, user=user)
        return method(system, user)
    return method(system, user)


def _render_chunk_user(
    group_id: str,
    member: str,
    messages: Sequence[Mapping[str, Any]],
    profile: Mapping[str, Any] | None,
    claims: Sequence[Mapping[str, Any]],
) -> str:
    serial_messages = [
        {
            "msg_id": int(row["msg_id"]),
            "group_id": row.get("group_id"),
            "sender_wxid": row.get("sender_wxid"),
            "sender_display": row.get("sender_display"),
            "t": row.get("t"),
            "type": row.get("type"),
            "content_text": row.get("content_text"),
            "transcript": row.get("transcript"),
            "quote_text": row.get("quote_text"),
        }
        for row in messages
    ]
    return (
        f"Update member knowledge for group_id={group_id!r}, sender_wxid={member!r}.\n"
        "Only derive facts from the messages below. Return one JSON object with "
        "optional profile (fixed sections), summary_text, and claims. Every profile "
        "section you update must have at least one claim in that same section. Each claim "
        "must include section, claim_text, basis, confidence (0..1), sensitive, "
        "and evidence_ids from the supplied msg_id values. You may supersede "
        "current claim ids for this same member.\n"
        f"CURRENT_PROFILE={_json_dump(profile or {})}\n"
        f"CURRENT_CLAIMS={_json_dump(list(claims))}\n"
        f"MESSAGES={_json_dump(serial_messages)}"
    )


def _select_messages_for_update(
    conn: sqlite3.Connection,
    group_id: str,
    member: str,
    cursor: int,
    until_msg_id: int | None,
) -> list[dict[str, Any]]:
    clauses = [
        "group_id=?",
        "msg_id>?",
        "(CASE WHEN sender_wxid IS NULL OR TRIM(sender_wxid)='' THEN ? ELSE TRIM(sender_wxid) END)=?",
    ]
    params: list[Any] = [group_id, int(cursor), UNKNOWN_MEMBER_ID, member]
    if until_msg_id is not None:
        clauses.append("msg_id<=?")
        params.append(int(until_msg_id))
    rows = conn.execute(
        "SELECT * FROM messages WHERE " + " AND ".join(clauses) + " ORDER BY t, msg_id",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def _chunk_by_chars(messages: Sequence[Mapping[str, Any]], chunk_chars: int) -> list[list[dict[str, Any]]]:
    budget = max(1, int(chunk_chars))
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    chars = 0
    for raw in messages:
        row = dict(raw)
        rendered = _message_render(row)
        size = len(rendered) + len(str(row.get("msg_id"))) + 32
        if current and chars + size > budget:
            chunks.append(current)
            current = []
            chars = 0
        current.append(row)
        chars += size
    if current:
        chunks.append(current)
    return chunks


def _fetch_state(conn: sqlite3.Connection, group_id: str, member: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM member_update_state WHERE group_id=? AND sender_wxid=?",
        (group_id, member),
    ).fetchone()


def _upsert_state(
    conn: sqlite3.Connection,
    group_id: str,
    member: str,
    *,
    cursor: int,
    cursor_t: int | None,
    full: bool,
    status: str,
    error: str | None,
    run_id: int | None,
    now: float,
) -> None:
    conn.execute(
        """
        INSERT INTO member_update_state
            (group_id,sender_wxid,cursor_msg_id,cursor_t,full_history_complete,
             last_status,last_error,last_run_id,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(group_id,sender_wxid) DO UPDATE SET
            cursor_msg_id=excluded.cursor_msg_id,
            cursor_t=excluded.cursor_t,
            full_history_complete=excluded.full_history_complete,
            last_status=excluded.last_status,
            last_error=excluded.last_error,
            last_run_id=excluded.last_run_id,
            updated_at=excluded.updated_at
        """,
        (group_id, member, cursor, cursor_t, int(full), status, error, run_id, now),
    )


def _apply_chunk(
    conn: sqlite3.Connection,
    group_id: str,
    member: str,
    chunk: Sequence[Mapping[str, Any]],
    parsed: Mapping[str, Any],
    run_id: int,
) -> None:
    now = _now()
    display = next((str(row.get("sender_display") or "").strip() for row in chunk if str(row.get("sender_display") or "").strip()), None)
    row = _ensure_profile(conn, group_id, member, display_name=display, now=now)
    profile = _json_load(row["profile_json"], _profile_defaults())
    if not isinstance(profile, dict):
        profile = _profile_defaults()
    for section in PROFILE_SECTIONS:
        profile.setdefault(section, None)
    locked = _json_load(row["locked_sections_json"], [])
    if not isinstance(locked, list):
        locked = []
    locked_set = {section for section in locked if section in _SECTION_SET}
    for section, value in dict(parsed.get("profile") or {}).items():
        if section not in locked_set:
            profile[section] = value
    summary = parsed.get("summary_text")
    if summary is None:
        summary = row["summary_text"] or ""
    conn.execute(
        """
        UPDATE member_profiles
           SET profile_json=?, summary_text=?, version=version+1,
               updated_at=?, deleted_at=NULL
         WHERE group_id=? AND sender_wxid=?
        """,
        (_json_dump(profile), summary, now, group_id, member),
    )
    # Superseding is explicit and auditable.  The validation step already
    # checked that every id is current for this exact member.
    supersede_ids = list(parsed.get("supersedes") or [])
    for claim_id in supersede_ids:
        conn.execute(
            "UPDATE member_claims SET status='superseded', superseded_by=NULL, updated_at=? WHERE claim_id=? AND group_id=? AND sender_wxid=? AND status='current'",
            (now, claim_id, group_id, member),
        )
    inserted_claim_ids: list[int] = []
    for claim in parsed.get("claims", []):
        # Exact duplicate current claims are idempotent when a caller rewinds
        # a cursor or retries a committed chunk.
        existing = conn.execute(
            """
            SELECT claim_id FROM member_claims
             WHERE group_id=? AND sender_wxid=? AND section=? AND claim_text=?
               AND status='current'
             ORDER BY claim_id DESC LIMIT 1
            """,
            (group_id, member, claim["section"], claim["claim_text"]),
        ).fetchone()
        if existing is not None:
            claim_id = int(existing["claim_id"])
        else:
            cur = conn.execute(
                """
                INSERT INTO member_claims
                    (group_id,sender_wxid,section,claim_text,basis,confidence,
                     sensitive,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    group_id,
                    member,
                    claim["section"],
                    claim["claim_text"],
                    claim["basis"],
                    claim["confidence"],
                    int(claim["sensitive"]),
                    claim["status"],
                    now,
                    now,
                ),
            )
            claim_id = int(cur.lastrowid)
        inserted_claim_ids.append(claim_id)
        for msg_id in claim.get("evidence_ids", []):
            conn.execute(
                "INSERT OR IGNORE INTO member_claim_evidence(claim_id,group_id,sender_wxid,msg_id,created_at) VALUES(?,?,?,?,?)",
                (claim_id, group_id, member, int(msg_id), now),
            )
        for old_id in claim.get("supersedes", []):
            conn.execute(
                "UPDATE member_claims SET status='superseded', superseded_by=?, updated_at=? WHERE claim_id=? AND group_id=? AND sender_wxid=?",
                (claim_id, now, old_id, group_id, member),
            )
    # Link an explicit supersede to the first replacement when possible.
    if supersede_ids and inserted_claim_ids:
        for old_id in supersede_ids:
            conn.execute(
                "UPDATE member_claims SET superseded_by=?, updated_at=? WHERE claim_id=? AND group_id=? AND sender_wxid=? AND status='superseded'",
                (inserted_claim_ids[0], now, old_id, group_id, member),
            )


def _update_run_row(conn: sqlite3.Connection, run_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields = dict(fields)
    assignments = ", ".join(f"{key}=?" for key in fields)
    conn.execute(
        f"UPDATE member_update_runs SET {assignments} WHERE run_id=?",
        [*fields.values(), run_id],
    )


def run_member_update(
    conn: sqlite3.Connection,
    group_id: str,
    sender_wxid: object,
    llm: Any,
    chunk_chars: int = 24_000,
    until_msg_id: int | None = None,
    retries: int = 3,
    sleep: Callable[[float], Any] = time.sleep,
) -> dict[str, Any]:
    """Update one member, committing each chronological chunk atomically."""

    _ensure_schema(conn)
    member = normalize_member_id(sender_wxid)
    now = _now()
    state = _fetch_state(conn, group_id, member)
    cursor_before = int(state["cursor_msg_id"] if state else 0)
    full_mode = not bool(state and int(state["full_history_complete"] or 0))
    mode = "full" if full_mode else "incremental"
    messages = _select_messages_for_update(conn, group_id, member, cursor_before, until_msg_id)
    if not messages:
        # A no-op does not call the model and does not create a misleading
        # cursor advance, but an audit row makes scheduler observations clear.
        with conn:
            _ensure_profile(conn, group_id, member, now=now)
            cur = conn.execute(
                """
                INSERT INTO member_update_runs
                    (group_id,sender_wxid,mode,status,cursor_before,cursor_after,
                     chunk_count,message_count,attempt_count,started_at,finished_at,details_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (group_id, member, mode, "skipped", cursor_before, cursor_before, 0, 0, 0, now, _now(), _json_dump({"reason": "no_new_messages"})),
            )
            run_id = int(cur.lastrowid)
            _upsert_state(
                conn,
                group_id,
                member,
                cursor=cursor_before,
                cursor_t=state["cursor_t"] if state else None,
                full=bool(state and state["full_history_complete"]),
                status="idle",
                error=None,
                run_id=run_id,
                now=_now(),
            )
        return {
            "status": "skipped",
            "reason": "no_new_messages",
            "group_id": group_id,
            "sender_wxid": member,
            "cursor_before": cursor_before,
            "cursor_after": cursor_before,
            "processed_messages": 0,
            "chunks": 0,
            "run_id": run_id,
        }

    chunks = _chunk_by_chars(messages, chunk_chars)
    # The state row references the profile.  Bootstrap an empty profile before
    # recording a running audit row so an API failure still leaves a resumable
    # cursor/error state without violating the FK.
    with conn:
        _ensure_profile(conn, group_id, member, now=now)
    with conn:
        cur = conn.execute(
            """
            INSERT INTO member_update_runs
                (group_id,sender_wxid,mode,status,cursor_before,chunk_count,message_count,started_at,details_json)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (group_id, member, mode, "running", cursor_before, len(chunks), len(messages), now, _json_dump({"until_msg_id": until_msg_id})),
        )
        run_id = int(cur.lastrowid)
        _upsert_state(
            conn,
            group_id,
            member,
            cursor=cursor_before,
            cursor_t=state["cursor_t"] if state else None,
            full=bool(state and state["full_history_complete"]),
            status="running",
            error=None,
            run_id=run_id,
            now=_now(),
        )

    attempts_total = 0
    processed = 0
    cursor_after = cursor_before
    chunk_results: list[dict[str, Any]] = []
    max_attempts = max(1, min(int(retries), 3))
    for index, chunk in enumerate(chunks):
        profile_row = conn.execute(
            "SELECT * FROM member_profiles WHERE group_id=? AND sender_wxid=?",
            (group_id, member),
        ).fetchone()
        profile = _profile_output(profile_row, conn) if profile_row else {"profile": _profile_defaults(), "summary_text": ""}
        existing_claims = conn.execute(
            "SELECT claim_id,section,claim_text,basis,confidence,sensitive,status FROM member_claims WHERE group_id=? AND sender_wxid=? AND status='current' ORDER BY claim_id",
            (group_id, member),
        ).fetchall()
        system = (
            "You maintain a durable per-member knowledge profile. Output strict JSON only. "
            "Do not invent evidence ids, group ids, or member ids. Fixed profile sections: "
            + ", ".join(PROFILE_SECTIONS)
            + ". Locked sections are preserved by the application."
        )
        user = _render_chunk_user(group_id, member, chunk, profile.get("profile") if profile else None, [dict(r) for r in existing_claims])
        parsed: dict[str, Any] | None = None
        last_error = ""
        for attempt in range(max_attempts):
            attempts_total += 1
            try:
                raw = _call_complete_json(llm, getattr(settings, "llm_model", ""), system, user)
                parsed = _validate_llm_output(raw, chunk, group_id, member, conn)
                break
            except Exception as exc:
                last_error = _safe_error(exc)
                if attempt + 1 < max_attempts:
                    try:
                        sleep(float(2**attempt))
                    except Exception:
                        # Test/injected sleepers should not make a failed LLM
                        # call look successful; continue without delaying.
                        pass
        if parsed is None:
            with conn:
                _update_run_row(
                    conn,
                    run_id,
                    status="failed",
                    attempt_count=attempts_total,
                    cursor_after=cursor_after,
                    error_text=last_error,
                    finished_at=_now(),
                )
                _upsert_state(
                    conn,
                    group_id,
                    member,
                    cursor=cursor_after,
                    cursor_t=(chunk[-1].get("t") if processed else (state["cursor_t"] if state else None)),
                    full=bool(state and state["full_history_complete"]),
                    status="failed",
                    error=last_error,
                    run_id=run_id,
                    now=_now(),
                )
            return {
                "status": "failed",
                "error": last_error,
                "group_id": group_id,
                "sender_wxid": member,
                "cursor_before": cursor_before,
                "cursor_after": cursor_after,
                "processed_messages": processed,
                "chunks": index,
                "run_id": run_id,
                "attempts": attempts_total,
            }
        # Profile/claims/evidence/cursor for one chunk share one transaction.
        with conn:
            _apply_chunk(conn, group_id, member, chunk, parsed, run_id)
            cursor_after = int(chunk[-1]["msg_id"])
            processed += len(chunk)
            complete = bool(index == len(chunks) - 1 and (until_msg_id is None or cursor_after >= int(until_msg_id)))
            _upsert_state(
                conn,
                group_id,
                member,
                cursor=cursor_after,
                cursor_t=int(chunk[-1].get("t") or 0),
                full=complete,
                status="running" if index < len(chunks) - 1 else "succeeded",
                error=None,
                run_id=run_id,
                now=_now(),
            )
            chunk_results.append({"index": index, "first_msg_id": int(chunk[0]["msg_id"]), "last_msg_id": cursor_after, "messages": len(chunk)})

    with conn:
        _update_run_row(
            conn,
            run_id,
            status="succeeded",
            attempt_count=attempts_total,
            cursor_after=cursor_after,
            finished_at=_now(),
            details_json=_json_dump({"chunks": chunk_results, "until_msg_id": until_msg_id}),
        )
    return {
        "status": "succeeded",
        "group_id": group_id,
        "sender_wxid": member,
        "cursor_before": cursor_before,
        "cursor_after": cursor_after,
        "processed_messages": processed,
        "chunks": len(chunks),
        "run_id": run_id,
        "attempts": attempts_total,
    }


def run_due_member_updates(
    conn: sqlite3.Connection,
    llm: Any,
    group_ids: Iterable[str] | None = None,
    chunk_chars: int = 24_000,
    until_msg_id: int | None = None,
    retries: int = 3,
    sleep: Callable[[float], Any] = time.sleep,
) -> dict[str, Any]:
    """Run independent updates for every active member in selected groups."""

    _ensure_schema(conn)
    groups = {str(item) for item in group_ids} if group_ids is not None else None
    rows = conn.execute(
        """
        SELECT DISTINCT group_id,
            CASE WHEN sender_wxid IS NULL OR TRIM(sender_wxid)='' THEN ? ELSE TRIM(sender_wxid) END AS sender_wxid
          FROM messages
         WHERE group_id IS NOT NULL
         ORDER BY group_id, sender_wxid
        """,
        (UNKNOWN_MEMBER_ID,),
    ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        if groups is not None and row["group_id"] not in groups:
            continue
        try:
            result = run_member_update(
                conn,
                row["group_id"],
                row["sender_wxid"],
                llm,
                chunk_chars=chunk_chars,
                until_msg_id=until_msg_id,
                retries=retries,
                sleep=sleep,
            )
        except Exception as exc:  # one member must not stop the rest
            logger.error("member update failed for isolated member job")
            result = {
                "status": "failed",
                "group_id": row["group_id"],
                "sender_wxid": row["sender_wxid"],
                "error": _safe_error(exc),
            }
        results.append(result)
    return {
        "status": "ok" if all(r.get("status") in {"succeeded", "skipped"} for r in results) else "partial",
        "results": results,
        "updated": sum(r.get("status") == "succeeded" for r in results),
        "skipped": sum(r.get("status") == "skipped" for r in results),
        "failed": sum(r.get("status") == "failed" for r in results),
    }


class MemberKnowledgeScheduler:
    """Crash-safe hourly scheduler for independent member updates."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        llm_factory: Any | None = None,
        settings_like: Any | None = None,
        *,
        grace_seconds: float | None = None,
        max_workers: int | None = None,
        **kwargs: Any,
    ) -> None:
        self.db_path = Path(db_path or getattr(settings_like or settings, "db_path", settings.db_path))
        self._settings = settings_like or settings
        self.llm_factory = llm_factory
        self.grace_seconds = float(
            grace_seconds
            if grace_seconds is not None
            else getattr(self._settings, "member_knowledge_grace_seconds", getattr(self._settings, "summary_sync_grace_seconds", 300))
        )
        self.max_workers = min(
            2,
            max(
                1,
                int(max_workers if max_workers is not None else getattr(self._settings, "member_kb_max_concurrency", 2)),
            ),
        )
        self.chunk_chars = int(getattr(self._settings, "member_kb_chunk_chars", 24_000))
        self.retries = min(3, max(1, int(getattr(self._settings, "member_kb_retries", 3))))
        self._executor = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="member-knowledge")
        self._futures: dict[tuple[str, str], Future[Any]] = {}
        self._lock = threading.Lock()
        self._last_submit: float | None = None
        self._closed = False

    def _make_llm(self, group_id: str, member: str) -> Any:
        factory = self.llm_factory
        if factory is None:
            raise RuntimeError("MemberKnowledgeScheduler requires llm_factory")
        if not callable(factory):
            return factory
        try:
            signature = inspect.signature(factory)
            names = list(signature.parameters)
        except (TypeError, ValueError):
            names = []
        if len(names) >= 2:
            return factory(group_id, member)
        if len(names) == 1:
            return factory(group_id)
        return factory()

    def _task(self, group_id: str, member: str, until_msg_id: int | None) -> dict[str, Any]:
        from .db import get_conn

        llm = self._make_llm(group_id, member)
        with get_conn(self.db_path) as conn:
            return run_member_update(
                conn,
                group_id,
                member,
                llm,
                chunk_chars=self.chunk_chars,
                retries=self.retries,
                until_msg_id=until_msg_id,
            )

    def maybe_submit(self, now: float | None = None) -> dict[str, Any]:
        """Submit mature member jobs; returns counts and observable errors."""

        if self._closed:
            return {"submitted": 0, "skipped": 0, "errors": ["scheduler closed"]}
        current = _now() if now is None else float(now)
        # A completed hour is eligible only after the grace interval has
        # elapsed.  At 12:05 with a 5-minute grace this is exactly 12:00, so
        # rows through 11:59:59 are mature (not merely rows through 11:55).
        mature_end = int((current - self.grace_seconds) // 3600) * 3600
        if mature_end <= 0:
            return {"submitted": 0, "skipped": 0, "errors": []}
        from .db import get_conn

        submitted = 0
        skipped = 0
        errors: list[str] = []
        try:
            with get_conn(self.db_path) as conn:
                where = ["1=1"]
                params: list[Any] = []
                allowed = getattr(self._settings, "groups", None)
                if allowed:
                    values = [str(item) for item in allowed if str(item).strip()]
                    if values:
                        placeholders = ",".join("?" * len(values))
                        where.append(
                            "(group_id IN (" + placeholders + ") OR group_name IN (" + placeholders + ") "
                            "OR EXISTS (SELECT 1 FROM group_aliases ga WHERE ga.alias_id=messages.group_id AND ga.group_name IN (" + placeholders + ")) "
                            "OR EXISTS (SELECT 1 FROM group_aliases ga WHERE ga.canonical_group_id=messages.group_id AND ga.group_name IN (" + placeholders + ")) "
                            "OR EXISTS (SELECT 1 FROM raw_group_authorizations ar WHERE ar.canonical_group_id=messages.group_id AND ar.display_name IN (" + placeholders + ")))"
                        )
                        params.extend(values * 5)
                if getattr(self._settings, "raw_wechat_enabled", False):
                    account = str(getattr(self._settings, "raw_wechat_account", "") or "")
                    where.append(
                        "EXISTS (SELECT 1 FROM raw_group_authorizations rga "
                        "WHERE rga.canonical_group_id=messages.group_id "
                        "AND rga.account_fingerprint=? AND rga.enabled=1)"
                    )
                    params.append(account)
                rows = conn.execute(
                    """
                    SELECT group_id,
                        CASE WHEN sender_wxid IS NULL OR TRIM(sender_wxid)='' THEN ? ELSE TRIM(sender_wxid) END AS sender_wxid,
                        MAX(msg_id) AS latest_msg_id,
                        MAX(CASE WHEN t < ? THEN msg_id END) AS mature_msg_id,
                        MAX(t) AS last_t
                      FROM messages
                     WHERE """ + " AND ".join(where) + """
                     GROUP BY group_id, CASE WHEN sender_wxid IS NULL OR TRIM(sender_wxid)='' THEN ? ELSE TRIM(sender_wxid) END
                    """,
                    [UNKNOWN_MEMBER_ID, mature_end, *params, UNKNOWN_MEMBER_ID],
                ).fetchall()
                for row in rows:
                    key = (str(row["group_id"]), str(row["sender_wxid"]))
                    state = _fetch_state(conn, *key)
                    full_history_complete = bool(
                        state is not None and int(state["full_history_complete"] or 0)
                    )
                    target_msg_id = (
                        row["mature_msg_id"]
                        if full_history_complete
                        else row["latest_msg_id"]
                    )
                    if target_msg_id is None:
                        skipped += 1
                        continue
                    if state is not None and int(state["cursor_msg_id"] or 0) >= int(target_msg_id):
                        skipped += 1
                        continue
                    with self._lock:
                        old = self._futures.get(key)
                        if old is not None and not old.done():
                            skipped += 1
                            continue
                        try:
                            future = self._executor.submit(self._task, key[0], key[1], int(target_msg_id))
                        except Exception as exc:
                            errors.append(_safe_error(exc))
                            continue
                        self._futures[key] = future
                        submitted += 1
        except Exception as exc:
            errors.append(_safe_error(exc))
        self._last_submit = current
        return {"submitted": submitted, "skipped": skipped, "errors": errors, "mature_until": mature_end}

    def status(self) -> dict[str, Any]:
        jobs: list[dict[str, Any]] = []
        errors: list[str] = []
        with self._lock:
            for key, future in list(self._futures.items()):
                item: dict[str, Any] = {"group_id": key[0], "sender_wxid": key[1], "done": future.done()}
                if future.done():
                    try:
                        item["result"] = future.result()
                    except Exception as exc:
                        item["error"] = _safe_error(exc)
                        errors.append(_safe_error(exc))
                jobs.append(item)
        return {"closed": self._closed, "last_submit": self._last_submit, "jobs": jobs, "errors": errors}

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._executor.shutdown(wait=True)


__all__ = [
    "UNKNOWN_MEMBER_ID",
    "PROFILE_SECTIONS",
    "normalize_member_id",
    "list_member_profiles",
    "get_member_profile",
    "search_member_profiles",
    "list_member_messages",
    "build_active_member_context",
    "update_member_profile_section",
    "delete_member_profile",
    "reset_member_profile",
    "run_member_update",
    "run_due_member_updates",
    "MemberKnowledgeScheduler",
]

"""Normalize a verified Weixin 4.1.11.55 group table into the main archive."""
from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Iterator
from itertools import chain
from pathlib import Path

from wechat_oracle.ingest.writer import write_messages
from wechat_oracle.ingest.group_identity import register_group_alias
from wechat_oracle.models import Message, MsgType
from wechat_oracle.db import transaction

EXPECTED_MESSAGE_COLUMNS = (
    "local_id", "server_id", "local_type", "sort_seq", "real_sender_id",
    "create_time", "status", "upload_status", "download_status", "server_seq",
    "origin_source", "source", "message_content", "compress_content",
    "packed_info_data", "WCDB_CT_message_content", "WCDB_CT_source",
)
OUTGOING_ORIGINS = {4, 5}
SHARD_ID = re.compile(r"^message_(\d+)")


def _shard_id(path: Path) -> str:
    match = SHARD_ID.match(path.name)
    return match.group(1) if match else path.stem


def _open_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro&immutable=1", uri=True)


def resolve_group(contact_db: Path, group_name: str) -> str:
    conn = _open_readonly(contact_db)
    try:
        rows = conn.execute(
            """
            SELECT username
              FROM contact
             WHERE username LIKE '%@chatroom'
               AND is_in_chat_room=1
               AND (nick_name=? OR remark=?)
            """,
            (group_name, group_name),
        ).fetchall()
    finally:
        conn.close()
    if len(rows) != 1:
        raise ValueError(f"group name must resolve exactly once; matches={len(rows)}")
    return str(rows[0][0])


def message_table(group_id: str) -> str:
    return "Msg_" + hashlib.md5(group_id.encode("utf-8")).hexdigest()  # noqa: S324 (schema naming, not security)


def _display_maps(message_db: Path, contact_db: Path) -> tuple[dict[int, str], dict[str, str]]:
    conn = _open_readonly(message_db)
    try:
        id_to_user = {int(row[0]): str(row[1]) for row in conn.execute("SELECT rowid, user_name FROM Name2Id")}
    finally:
        conn.close()
    conn = _open_readonly(contact_db)
    try:
        display_by_user = {
            str(row[0]): str(row[1] or row[2] or row[3] or row[0])
            for row in conn.execute("SELECT username, remark, nick_name, alias FROM contact")
        }
    finally:
        conn.close()
    return id_to_user, display_by_user


def iter_group_text_messages(
    message_db: Path,
    contact_db: Path,
    *,
    group_id: str,
    group_name: str,
    since_t: int | None = None,
    shard_id: str | None = None,
    after_local_id: int | None = None,
) -> Iterator[Message]:
    """Yield exact text rows; no raw content is logged or persisted elsewhere."""
    table = message_table(group_id)
    id_to_user, display_by_user = _display_maps(message_db, contact_db)
    conn = _open_readonly(message_db)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if exists is None:
            return
        columns = tuple(row[1] for row in conn.execute(f'PRAGMA table_info("{table}")'))
        if columns != EXPECTED_MESSAGE_COLUMNS:
            raise ValueError("message table does not match the exact 4.1.11.55 schema")
        sql = (
            f'SELECT local_id, server_id, real_sender_id, create_time, origin_source, message_content '
            f'FROM "{table}" WHERE local_type=1'
        )
        params: list[int] = []
        if after_local_id is not None:
            sql += " AND local_id>?"
            params.append(after_local_id)
        if since_t is not None:
            sql += " AND create_time>=?"
            params.append(since_t)
        sql += " ORDER BY local_id"
        for local_id, server_id, sender_id, create_time, origin_source, content in conn.execute(sql, params):
            if isinstance(content, bytes):
                try:
                    content = content.decode("utf-8")
                except UnicodeDecodeError:
                    continue
            if not isinstance(content, str) or not content.strip():
                continue
            outgoing = int(origin_source or 0) in OUTGOING_ORIGINS
            sender_wxid = None if outgoing else id_to_user.get(int(sender_id or 0))
            local_namespace = shard_id or message_db.stem
            wx_message_id = (
                str(server_id)
                if int(server_id or 0)
                else f"local:{local_namespace}:{local_id}"
            )
            yield Message(
                wx_msg_id=wx_message_id,
                group_id=group_id,
                group_name=group_name,
                sender_wxid=sender_wxid,
                sender_display="我" if outgoing else display_by_user.get(sender_wxid or "", sender_wxid),
                t=int(create_time),
                type=MsgType.TEXT,
                content_text=content,
                source="backfill",
            )
    finally:
        conn.close()


def import_group_text_messages(
    archive: sqlite3.Connection,
    message_db: Path,
    contact_db: Path,
    *,
    group_name: str,
    since_t: int | None = None,
) -> tuple[str, int, int]:
    group_id = resolve_group(contact_db, group_name)
    with transaction(archive):
        register_group_alias(archive, group_name=group_name, canonical_id=group_id)
    attempted, inserted = write_messages(
        archive,
        iter_group_text_messages(
            message_db,
            contact_db,
            group_id=group_id,
            group_name=group_name,
            since_t=since_t,
        ),
    )
    return group_id, attempted, inserted


def import_group_text_messages_many(
    archive: sqlite3.Connection,
    message_dbs: list[Path],
    contact_db: Path,
    *,
    group_name: str,
    since_t: int | None = None,
) -> tuple[str, int, int]:
    """Import one group across every reviewed WeChat 4 message shard."""
    group_id, attempted, inserted, _ = import_group_text_messages_many_with_cursors(
        archive,
        message_dbs,
        contact_db,
        group_name=group_name,
        since_t=since_t,
    )
    return group_id, attempted, inserted


def import_group_text_messages_many_with_cursors(
    archive: sqlite3.Connection,
    message_dbs: list[Path],
    contact_db: Path,
    *,
    group_name: str,
    after_local_ids: dict[str, int] | None = None,
    since_t: int | None = None,
) -> tuple[str, int, int, dict[str, int]]:
    """Import shards and return safe post-commit local-id high-water marks."""
    if not message_dbs:
        raise ValueError("at least one message shard is required")
    group_id = resolve_group(contact_db, group_name)
    with transaction(archive):
        register_group_alias(archive, group_name=group_name, canonical_id=group_id)
    after_local_ids = after_local_ids or {}
    streams = (
        iter_group_text_messages(
            message_db,
            contact_db,
            group_id=group_id,
            group_name=group_name,
            since_t=since_t,
            shard_id=_shard_id(message_db),
            after_local_id=after_local_ids.get(_shard_id(message_db)),
        )
        for message_db in message_dbs
    )
    attempted, inserted = write_messages(archive, chain.from_iterable(streams))
    cursors: dict[str, int] = {}
    table = message_table(group_id)
    for message_db in message_dbs:
        conn = _open_readonly(message_db)
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if exists is None:
                continue
            row = conn.execute(f'SELECT MAX(local_id) FROM "{table}"').fetchone()
            if row is not None and row[0] is not None:
                cursors[_shard_id(message_db)] = int(row[0])
        finally:
            conn.close()
    return group_id, attempted, inserted, cursors

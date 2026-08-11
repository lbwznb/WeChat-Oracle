"""Canonicalize UI-only group identities once a real chatroom id is known."""
from __future__ import annotations

import hashlib
import sqlite3
import time


def ui_group_id(group_name: str) -> str:
    digest = hashlib.sha256(group_name.encode("utf-8")).hexdigest()[:16]
    return f"ui:{digest}"


def canonical_group_id(conn: sqlite3.Connection, group_name: str) -> str:
    alias = ui_group_id(group_name)
    row = conn.execute(
        "SELECT canonical_group_id FROM group_aliases WHERE alias_id=?",
        (alias,),
    ).fetchone()
    return str(row[0]) if row is not None else alias


def register_group_alias(
    conn: sqlite3.Connection,
    *,
    group_name: str,
    canonical_id: str,
) -> str:
    alias = ui_group_id(group_name)
    existing = conn.execute(
        "SELECT canonical_group_id FROM group_aliases WHERE alias_id=?",
        (alias,),
    ).fetchone()
    if existing is not None and str(existing[0]) != canonical_id:
        raise ValueError("group display-name alias is already bound to another canonical chatroom")
    conn.execute(
        """
        INSERT INTO group_aliases(alias_id, canonical_group_id, group_name, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(alias_id) DO UPDATE SET
            group_name=excluded.group_name,
            updated_at=excluded.updated_at
        """,
        (alias, canonical_id, group_name, time.time()),
    )
    return alias

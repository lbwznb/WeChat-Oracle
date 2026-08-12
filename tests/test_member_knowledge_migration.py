from __future__ import annotations

import sqlite3

from wechat_oracle.db import get_conn, init_db


def test_member_knowledge_migration_is_additive(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE messages (
            msg_id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT NOT NULL,
            sender_wxid TEXT,
            sender_display TEXT,
            t INTEGER NOT NULL,
            type TEXT NOT NULL,
            content_text TEXT,
            source TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'raw',
            dedupe_key TEXT NOT NULL UNIQUE,
            created_at INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO messages(group_id,sender_wxid,t,type,content_text,source,dedupe_key)
        VALUES('g','u',1,'text','keep','live','legacy-row');
        CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO schema_meta(key,value) VALUES('version','1');
        """
    )
    conn.commit()
    conn.close()

    init_db(path)
    with get_conn(path) as migrated:
        names = {
            row[0]
            for row in migrated.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "messages",
            "member_profiles",
            "member_alias_history",
            "member_claims",
            "member_claim_evidence",
            "member_update_state",
            "member_update_runs",
        } <= names
        indexes = {
            row[1]
            for row in migrated.execute("PRAGMA index_list(messages)")
        }
        assert "idx_messages_group_sender_msg" in indexes
        assert migrated.execute("SELECT content_text FROM messages").fetchone()[0] == "keep"
        assert migrated.execute(
            "SELECT value FROM schema_meta WHERE key='version'"
        ).fetchone()[0] == "5"
        assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []

from __future__ import annotations

import sqlite3

from wechat_oracle.db import get_conn, init_db


def test_v3_summary_rows_survive_hourly_schema_migration(tmp_path) -> None:
    db_path = tmp_path / "archive.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE summary_runs (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                group_name TEXT,
                period_start INTEGER NOT NULL,
                period_end INTEGER NOT NULL,
                trigger_kind TEXT NOT NULL CHECK(trigger_kind IN ('daily', 'manual')),
                status TEXT NOT NULL CHECK(status IN ('running', 'skipped', 'ready', 'sent', 'failed', 'unknown')),
                message_count INTEGER NOT NULL DEFAULT 0,
                summary_text TEXT,
                result TEXT NOT NULL DEFAULT '',
                started_at REAL NOT NULL,
                finished_at REAL,
                UNIQUE(group_id, period_start, period_end, trigger_kind)
            );
            CREATE TABLE delivery_outbox (
                delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
                summary_run_id INTEGER NOT NULL UNIQUE REFERENCES summary_runs(run_id) ON DELETE CASCADE,
                status TEXT NOT NULL CHECK(status IN ('pending', 'sending', 'sent', 'failed', 'unknown')),
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            INSERT INTO summary_runs
                (run_id, group_id, group_name, period_start, period_end,
                 trigger_kind, status, message_count, summary_text, result,
                 started_at, finished_at)
            VALUES (7, 'g', '群', 10, 20, 'daily', 'sent', 12, '旧摘要', '', 21, 22);
            INSERT INTO delivery_outbox
                (delivery_id, summary_run_id, status, attempt_count,
                 last_error, created_at, updated_at)
            VALUES (9, 7, 'sent', 1, '', 21, 22);
            """
        )

    init_db(db_path)

    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT run_id, trigger_kind, status, summary_text, generation_attempt_count "
            "FROM summary_runs WHERE run_id=7"
        ).fetchone()
        assert tuple(row) == (7, "daily", "sent", "旧摘要", 1)
        outbox = conn.execute(
            "SELECT delivery_id, summary_run_id, status FROM delivery_outbox WHERE delivery_id=9"
        ).fetchone()
        assert tuple(outbox) == (9, 7, "sent")
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.execute(
            """
            INSERT INTO summary_runs
                (group_id, period_start, period_end, trigger_kind, status, started_at, updated_at)
            VALUES ('g', 20, 30, 'hourly', 'running', 30, 30)
            """
        )

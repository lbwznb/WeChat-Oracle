"""SQLite connection + schema bootstrap + transaction helper.

WAL mode is essential — the writer process (`ingest live`), the reader
(`dispatcher`), and any ad-hoc CLI command share the same DB file. WAL
allows concurrent readers + one writer.

`init_db()` runs `schema.sql` (idempotent thanks to `IF NOT EXISTS`); call
it from any entry point that touches the DB. `transaction()` is the only
sanctioned way to write — it manages BEGIN/COMMIT/ROLLBACK explicitly
because we set `isolation_level=None` (autocommit) on the connection.
"""
import sqlite3
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path
from typing import Iterator

from .config import settings

SCHEMA_RESOURCE = "schema.sql"


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None)  # autocommit; we manage txns explicitly
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def init_db(db_path: Path | None = None) -> Path:
    path = db_path or settings.db_path
    settings.ensure_dirs()
    schema_sql = files("wechat_oracle").joinpath(SCHEMA_RESOURCE).read_text(encoding="utf-8")
    with _connect(path) as conn:
        conn.executescript(schema_sql)
        _migrate(conn)
    return path


def _migrate(conn: sqlite3.Connection) -> None:
    """One-shot column adds for installs that ran an older schema. Each
    block is idempotent and only fires when the column is actually missing,
    so this is safe to re-run."""
    def _has_column(table: str, col: str) -> bool:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(r["name"] == col for r in rows)

    _migrate_summary_schema(conn)

    # last_run_id added when group_memory landed (persona_drift now has the
    # back-pointer too — see schema.sql comment)
    if not _has_column("persona_drift", "last_run_id"):
        conn.execute("ALTER TABLE persona_drift ADD COLUMN last_run_id INTEGER")
    if not _has_column("forwarded_records", "media_path"):
        conn.execute("ALTER TABLE forwarded_records ADD COLUMN media_path TEXT")
    conn.execute(
        """
        UPDATE forwarded_records
           SET media_path = (
               SELECT m.media_path
                 FROM messages m
                WHERE m.wx_msg_id = forwarded_records.src_msg_id
                  AND m.type = 'image'
                  AND m.media_path IS NOT NULL
                ORDER BY m.msg_id DESC
                LIMIT 1
           )
         WHERE media_path IS NULL
           AND datatype = 2
           AND src_msg_id IS NOT NULL
           AND EXISTS (
               SELECT 1 FROM messages m
                WHERE m.wx_msg_id = forwarded_records.src_msg_id
                  AND m.type = 'image'
                  AND m.media_path IS NOT NULL
           )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_lurk_state (
            group_id       TEXT PRIMARY KEY,
            last_msg_id    INTEGER,
            last_run_id    INTEGER,
            updated_at     REAL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_proactive_outbox (
            job_id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id               TEXT NOT NULL,
            group_name             TEXT,
            kind                   TEXT NOT NULL CHECK(kind IN ('committed', 'thread')),
            status                 TEXT NOT NULL CHECK(status IN ('planned', 'pending', 'running', 'sent', 'cancelled', 'expired', 'failed')),
            continuation_token     TEXT NOT NULL,
            source_run_id          INTEGER,
            source_trigger_msg_id  INTEGER,
            source_trigger_kind    TEXT,
            source_job_id          INTEGER,
            sequence               INTEGER NOT NULL DEFAULT 1,
            max_sequence           INTEGER NOT NULL DEFAULT 1,
            intent                 TEXT NOT NULL,
            reason                 TEXT NOT NULL DEFAULT '',
            delay_seconds          INTEGER NOT NULL DEFAULT 90,
            scheduled_at           REAL NOT NULL,
            expires_at             REAL NOT NULL,
            anchor_msg_id          INTEGER,
            latest_msg_id          INTEGER,
            created_at             REAL NOT NULL,
            updated_at             REAL NOT NULL,
            result                 TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_outbox_due "
        "ON agent_proactive_outbox(status, scheduled_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_outbox_group_status "
        "ON agent_proactive_outbox(group_id, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_outbox_token "
        "ON agent_proactive_outbox(continuation_token)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS summary_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT NOT NULL,
            group_name TEXT,
            period_start INTEGER NOT NULL,
            period_end INTEGER NOT NULL,
            trigger_kind TEXT NOT NULL CHECK(trigger_kind IN ('hourly', 'daily', 'manual')),
            status TEXT NOT NULL CHECK(status IN ('running', 'skipped', 'ready', 'sent', 'failed', 'unknown')),
            message_count INTEGER NOT NULL DEFAULT 0,
            summary_text TEXT,
            result TEXT NOT NULL DEFAULT '',
            started_at REAL NOT NULL,
            finished_at REAL,
            generation_attempt_count INTEGER NOT NULL DEFAULT 1,
            lease_token TEXT,
            lease_until REAL,
            updated_at REAL NOT NULL DEFAULT 0,
            UNIQUE(group_id, period_start, period_end, trigger_kind)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_summary_runs_period "
        "ON summary_runs(period_end, status)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS delivery_outbox (
            delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
            summary_run_id INTEGER NOT NULL UNIQUE,
            status TEXT NOT NULL CHECK(status IN ('pending', 'sending', 'sent', 'failed', 'unknown')),
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_delivery_outbox_status "
        "ON delivery_outbox(status, updated_at)"
    )
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('version', '4') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )


def _migrate_summary_schema(conn: sqlite3.Connection) -> None:
    """Rebuild v3 summary tables so existing installs accept hourly jobs."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='summary_runs'"
    ).fetchone()
    if row is None:
        return
    sql = str(row["sql"] or "")
    columns = {item["name"] for item in conn.execute("PRAGMA table_info(summary_runs)")}
    required = {"generation_attempt_count", "lease_token", "lease_until", "updated_at"}
    if "'hourly'" in sql and required.issubset(columns):
        return

    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.executescript(
            """
            BEGIN IMMEDIATE;
            ALTER TABLE delivery_outbox RENAME TO delivery_outbox_v3;
            ALTER TABLE summary_runs RENAME TO summary_runs_v3;

            CREATE TABLE summary_runs (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                group_name TEXT,
                period_start INTEGER NOT NULL,
                period_end INTEGER NOT NULL,
                trigger_kind TEXT NOT NULL CHECK(trigger_kind IN ('hourly', 'daily', 'manual')),
                status TEXT NOT NULL CHECK(status IN ('running', 'skipped', 'ready', 'sent', 'failed', 'unknown')),
                message_count INTEGER NOT NULL DEFAULT 0,
                summary_text TEXT,
                result TEXT NOT NULL DEFAULT '',
                started_at REAL NOT NULL,
                finished_at REAL,
                generation_attempt_count INTEGER NOT NULL DEFAULT 1,
                lease_token TEXT,
                lease_until REAL,
                updated_at REAL NOT NULL DEFAULT 0,
                UNIQUE(group_id, period_start, period_end, trigger_kind)
            );
            INSERT INTO summary_runs (
                run_id, group_id, group_name, period_start, period_end,
                trigger_kind, status, message_count, summary_text, result,
                started_at, finished_at, generation_attempt_count, updated_at
            )
            SELECT run_id, group_id, group_name, period_start, period_end,
                   trigger_kind, status, message_count, summary_text, result,
                   started_at, finished_at, 1, COALESCE(finished_at, started_at)
              FROM summary_runs_v3;

            CREATE TABLE delivery_outbox (
                delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
                summary_run_id INTEGER NOT NULL UNIQUE REFERENCES summary_runs(run_id) ON DELETE CASCADE,
                status TEXT NOT NULL CHECK(status IN ('pending', 'sending', 'sent', 'failed', 'unknown')),
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            INSERT INTO delivery_outbox (
                delivery_id, summary_run_id, status, attempt_count,
                last_error, created_at, updated_at
            )
            SELECT delivery_id, summary_run_id, status, attempt_count,
                   last_error, created_at, updated_at
              FROM delivery_outbox_v3;

            DROP TABLE delivery_outbox_v3;
            DROP TABLE summary_runs_v3;
            CREATE INDEX idx_summary_runs_period ON summary_runs(period_end, status);
            CREATE INDEX idx_delivery_outbox_status ON delivery_outbox(status, updated_at);
            COMMIT;
            """
        )
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError("summary schema migration left invalid foreign keys")


@contextmanager
def get_conn(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    path = db_path or settings.db_path
    conn = _connect(path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")

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
        _prepare_legacy_messages(conn)
        conn.executescript(schema_sql)
        _migrate(conn)
    return path


def _prepare_legacy_messages(conn: sqlite3.Connection) -> None:
    """Add columns required by the additive schema before its indexes run.

    Very early archives only had the core ``group_id/t/type/content`` fields;
    ``CREATE INDEX ... wx_msg_id`` in the modern schema would otherwise fail
    before :func:`_migrate` gets a chance to run.  Adding nullable/defaulted
    columns preserves every existing row and leaves all raw content intact.
    """

    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'"
    ).fetchone()
    if table is None:
        return
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
    additions: dict[str, str] = {
        "wx_msg_id": "TEXT",
        "group_name": "TEXT",
        "sender_wxid": "TEXT",
        "sender_display": "TEXT",
        "content_text": "TEXT",
        "media_path": "TEXT",
        "reply_to_wx_msg_id": "TEXT",
        "quote_text": "TEXT",
        "transcript": "TEXT",
        "source": "TEXT NOT NULL DEFAULT 'backfill'",
        "status": "TEXT NOT NULL DEFAULT 'raw'",
        "dedupe_key": "TEXT NOT NULL DEFAULT ''",
        "created_at": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, declaration in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {name} {declaration}")


def _migrate(conn: sqlite3.Connection) -> None:
    """One-shot column adds for installs that ran an older schema. Each
    block is idempotent and only fires when the column is actually missing,
    so this is safe to re-run."""
    def _has_column(table: str, col: str) -> bool:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(r["name"] == col for r in rows)

    _migrate_summary_schema(conn)

    # Member knowledge is an additive feature.  Keep the migration here as
    # well as in schema.sql so an existing connection that was initialized
    # from an older schema receives the composite message index and all
    # durable derived-state tables without any destructive rebuild.
    _migrate_member_knowledge(conn)

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
        "INSERT INTO schema_meta(key, value) VALUES('version', '5') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )


def _migrate_member_knowledge(conn: sqlite3.Connection) -> None:
    """Create per-member knowledge tables/indexes for pre-feature installs."""

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_group_sender_msg "
        "ON messages(group_id, sender_wxid, msg_id)"
    )
    conn.executescript(
        """
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
                'identity','interests','skills','communication_style','habits',
                'relationships','opinions','sensitive_inferences','recent_focus'
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
                REFERENCES member_profiles(group_id, sender_wxid) ON DELETE CASCADE
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
            FOREIGN KEY(claim_id) REFERENCES member_claims(claim_id) ON DELETE CASCADE,
            FOREIGN KEY(msg_id) REFERENCES messages(msg_id) ON DELETE CASCADE
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
                REFERENCES member_profiles(group_id, sender_wxid) ON DELETE CASCADE
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

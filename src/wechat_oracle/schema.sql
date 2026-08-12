-- WeChat-Oracle SQLite schema (v3)
-- Single source of truth for normalized chat messages.
-- Status field drives the downstream pipeline (mm -> segmenter -> indexer).

CREATE TABLE IF NOT EXISTS messages (
    msg_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    wx_msg_id           TEXT,           -- WeChat MsgSvrID; present for backfill, often null for live
    group_id            TEXT NOT NULL,  -- group's wxid (backfill) or display-name fallback (live)
    group_name          TEXT,
    sender_wxid         TEXT,
    sender_display      TEXT,           -- 群昵称 / 备注 / 微信昵称, in that priority
    t                   INTEGER NOT NULL,  -- unix seconds, UTC
    type                TEXT NOT NULL,     -- text|image|voice|video|link|forward|quote|sticker|system
    content_text        TEXT,           -- normalized visible text, card preview, or media placeholder
    media_path          TEXT,           -- data_dir-relative path under media/<group>/<kind>/<filename>
    reply_to_wx_msg_id  TEXT,           -- parent's wx_msg_id when this is a quote/reply
    quote_text          TEXT,           -- snippet of quoted msg, when wxauto can't resolve parent id
    transcript          TEXT,           -- OCR/ASR output for media (image/voice). NULL = not yet processed; '' = processed, no text found
    source              TEXT NOT NULL CHECK (source IN ('live', 'backfill')),
    status              TEXT NOT NULL DEFAULT 'raw'
                        CHECK (status IN ('raw', 'mm_pending', 'mm_done', 'assigned', 'indexed')),
    dedupe_key          TEXT NOT NULL,  -- per-source-determined; used to avoid duplicate inserts
    created_at          INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    UNIQUE (dedupe_key)
);

CREATE INDEX IF NOT EXISTS idx_messages_group_t       ON messages (group_id, t);
CREATE INDEX IF NOT EXISTS idx_messages_status        ON messages (status);
CREATE INDEX IF NOT EXISTS idx_messages_sender        ON messages (sender_wxid);
CREATE INDEX IF NOT EXISTS idx_messages_wx_msg_id     ON messages (wx_msg_id);
-- Member-knowledge reads always scope by group + normalized sender and then
-- walk the immutable message id cursor.  Keep the raw sender value unchanged;
-- NULL/blank values are normalized to __unknown__ by the knowledge layer.
CREATE INDEX IF NOT EXISTS idx_messages_group_sender_msg
    ON messages (group_id, sender_wxid, msg_id);

-- Maps display-name-derived UI ids onto a verified real @chatroom id. This
-- keeps raw history, UI live events, dispatcher context, and daily summaries
-- in one canonical group without exposing account metadata.
CREATE TABLE IF NOT EXISTS group_aliases (
    alias_id            TEXT PRIMARY KEY,
    canonical_group_id  TEXT NOT NULL,
    group_name          TEXT NOT NULL,
    updated_at          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_group_aliases_canonical
    ON group_aliases(canonical_group_id);

-- Explicit local-WeChat read authorization. The display name is informational;
-- the security boundary is the anonymous account fingerprint + real chatroom id.
CREATE TABLE IF NOT EXISTS raw_group_authorizations (
    account_fingerprint TEXT NOT NULL,
    canonical_group_id  TEXT NOT NULL,
    display_name        TEXT NOT NULL,
    contact_generation TEXT NOT NULL,
    enabled             INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    created_at          REAL NOT NULL,
    updated_at          REAL NOT NULL,
    PRIMARY KEY(account_fingerprint, canonical_group_id)
);

CREATE TABLE IF NOT EXISTS raw_import_cursors (
    account_fingerprint TEXT NOT NULL,
    canonical_group_id  TEXT NOT NULL,
    shard_id             TEXT NOT NULL,
    database_generation TEXT NOT NULL,
    last_local_id        INTEGER NOT NULL DEFAULT 0,
    updated_at           REAL NOT NULL,
    PRIMARY KEY(account_fingerprint, canonical_group_id, shard_id),
    FOREIGN KEY(account_fingerprint, canonical_group_id)
        REFERENCES raw_group_authorizations(account_fingerprint, canonical_group_id)
        ON DELETE CASCADE
);

-- Lightweight per-group state: cursor for incremental backfill, last-seen for live polling.
CREATE TABLE IF NOT EXISTS group_state (
    group_id            TEXT PRIMARY KEY,
    group_name          TEXT,
    last_backfill_t     INTEGER,        -- max(t) successfully imported via backfill
    last_live_t         INTEGER,        -- max(t) seen by live poller
    last_live_dedupe    TEXT
);

-- Children of WeChat 合并转发 (merged-forward) messages. The wrapper itself lives
-- in `messages` as type='forward'; this table holds each `<dataitem>` from its
-- `<recordinfo>` XML, flattened one level (nested forwards are placeholder only).
-- sender_display comes from `<sourcename>` (no wxid available; `<hashusername>`
-- is sha256 and not invertible). timestamp is `<srcMsgCreateTime>` of the
-- original message in its source group, NOT the time of the forward.
CREATE TABLE IF NOT EXISTS forwarded_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_msg_id   INTEGER NOT NULL REFERENCES messages(msg_id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,    -- 0-based ordinal within the bundle
    sender_display  TEXT,                -- sourcename (display name; no wxid)
    t               INTEGER NOT NULL,    -- srcMsgCreateTime (Unix sec)
    datatype        INTEGER NOT NULL,    -- 1=text; others get placeholder content
    content         TEXT,                -- html-unescaped datadesc, or "[图片]" etc.
    src_msg_id      TEXT,                -- fromnewmsgid (informational; not joined)
    media_path      TEXT,                -- data_dir-relative media path for child media when available
    UNIQUE(parent_msg_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_fwd_records_parent ON forwarded_records(parent_msg_id);
CREATE INDEX IF NOT EXISTS idx_fwd_records_t      ON forwarded_records(t);
CREATE INDEX IF NOT EXISTS idx_fwd_records_sender ON forwarded_records(sender_display);

-- Tracks dispatcher runs: which incoming command messages have been processed.
-- Decoupled from `messages.status` so the message lifecycle stays clean.
CREATE TABLE IF NOT EXISTS command_runs (
    msg_id      INTEGER PRIMARY KEY,
    started_at  INTEGER NOT NULL,
    finished_at INTEGER,
    status      TEXT NOT NULL CHECK (status IN ('running', 'ok', 'error')),
    result      TEXT
);

-- Idempotent automatic summaries and their delivery state. `unknown` means
-- the UI send may have happened; it is deliberately never auto-retried.
CREATE TABLE IF NOT EXISTS summary_runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id        TEXT NOT NULL,
    group_name      TEXT,
    period_start    INTEGER NOT NULL,
    period_end      INTEGER NOT NULL,
    trigger_kind    TEXT NOT NULL CHECK(trigger_kind IN ('hourly', 'daily', 'manual')),
    status          TEXT NOT NULL CHECK(status IN ('running', 'skipped', 'ready', 'sent', 'failed', 'unknown')),
    message_count   INTEGER NOT NULL DEFAULT 0,
    summary_text    TEXT,
    result          TEXT NOT NULL DEFAULT '',
    started_at      REAL NOT NULL,
    finished_at     REAL,
    generation_attempt_count INTEGER NOT NULL DEFAULT 1,
    lease_token     TEXT,
    lease_until     REAL,
    updated_at      REAL NOT NULL DEFAULT 0,
    UNIQUE(group_id, period_start, period_end, trigger_kind)
);
CREATE INDEX IF NOT EXISTS idx_summary_runs_period
    ON summary_runs(period_end, status);

CREATE TABLE IF NOT EXISTS delivery_outbox (
    delivery_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    summary_run_id  INTEGER NOT NULL UNIQUE REFERENCES summary_runs(run_id) ON DELETE CASCADE,
    status          TEXT NOT NULL CHECK(status IN ('pending', 'sending', 'sent', 'failed', 'unknown')),
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_delivery_outbox_status
    ON delivery_outbox(status, updated_at);

-- ---------- Agent loop + memory (CLAUDE.md F17, see plan in commit history) ----------
-- The dispatcher's @<bot> chat path runs through a multi-turn tool-calling agent
-- loop (`src/wechat_oracle/agent/`). Memory is two evolvable blobs per group +
-- a full audit trail of every agent run.

-- Per-group evolvable persona supplement (the static core lives in
-- data/personas/<group_id>.yaml). Replaced wholesale by `update_persona_drift`.
-- `last_run_id` points back into agent_run_log so any state in this row can be
-- traced to the run that wrote it (combats summarization-drift, in the spirit
-- of "keep raw memories linked to consolidated memories").
CREATE TABLE IF NOT EXISTS persona_drift (
    group_id     TEXT PRIMARY KEY,
    drift_text   TEXT NOT NULL DEFAULT '',
    updated_at   REAL,
    last_run_id  INTEGER REFERENCES agent_run_log(run_id) ON DELETE SET NULL
);

-- Single per-group memory blob holding everything the agent has learned about
-- members, group culture, and recurring topics — one freeform document, agent's
-- job to organize internally. Replaces the previous (member_notes,
-- group_notes) pair: per-id modeling turned out to be over-structured for what
-- the agent actually needs. Hard-capped by WO_AGENT_MEMORY_MAX_CHARS (default
-- 100000) so the agent has to compact when it runs out of room.
CREATE TABLE IF NOT EXISTS group_memory (
    group_id     TEXT PRIMARY KEY,
    notes_text   TEXT NOT NULL DEFAULT '',
    size_chars   INTEGER NOT NULL DEFAULT 0,
    updated_at   REAL,
    last_run_id  INTEGER REFERENCES agent_run_log(run_id) ON DELETE SET NULL
);

-- Audit trace of every agent run. Chat runs store full `phase_a_trace` /
-- `phase_b_trace`; lurk stores a compact observation trace plus tool trace.
-- Trace columns are JSON arrays of step dicts:
-- `[{step:int, kind:'tool_call'|'final', tool?, args?, result?, content?}, ...]`.
-- `reply_text` is NULL when the agent chose stay_silent. `trigger_kind` is one
-- of 'mention' / 'reply' / 'probability' / 'proactive_followup' / 'lurk' /
-- 'local_ask' / 'local_task'.
CREATE TABLE IF NOT EXISTS agent_run_log (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id        TEXT NOT NULL,
    trigger_msg_id  INTEGER,
    trigger_kind    TEXT,
    phase_a_trace   TEXT,
    phase_b_trace   TEXT,
    reply_text      TEXT,
    started_at      REAL,
    finished_at     REAL
);
CREATE INDEX IF NOT EXISTS idx_agent_log_group_t ON agent_run_log(group_id, started_at);

-- Per-group cursor for silent background learning. This is operational state,
-- deliberately separate from agent_run_log: audit rows describe what happened;
-- this row says where the next lurk pass should resume.
CREATE TABLE IF NOT EXISTS agent_lurk_state (
    group_id       TEXT PRIMARY KEY,
    last_msg_id    INTEGER,
    last_run_id    INTEGER REFERENCES agent_run_log(run_id) ON DELETE SET NULL,
    updated_at     REAL
);

-- Delayed proactive continuations. Agent turns may schedule a follow-up by
-- intent, but no message is pre-generated: dispatcher reruns the agent when
-- the row becomes due. `planned` rows are created during the source agent run;
-- they become `pending` only after the source reply is successfully sent.
CREATE TABLE IF NOT EXISTS agent_proactive_outbox (
    job_id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id               TEXT NOT NULL,
    group_name             TEXT,
    kind                   TEXT NOT NULL CHECK(kind IN ('committed', 'thread')),
    status                 TEXT NOT NULL CHECK(status IN ('planned', 'pending', 'running', 'sent', 'cancelled', 'expired', 'failed')),
    continuation_token     TEXT NOT NULL,
    source_run_id          INTEGER REFERENCES agent_run_log(run_id) ON DELETE SET NULL,
    source_trigger_msg_id  INTEGER,
    source_trigger_kind    TEXT,
    source_job_id          INTEGER REFERENCES agent_proactive_outbox(job_id) ON DELETE SET NULL,
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
);
CREATE INDEX IF NOT EXISTS idx_agent_outbox_due
    ON agent_proactive_outbox(status, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_agent_outbox_group_status
    ON agent_proactive_outbox(group_id, status);
CREATE INDEX IF NOT EXISTS idx_agent_outbox_token
    ON agent_proactive_outbox(continuation_token);

-- Legacy memory tables kept as inert compatibility tables for old installs.
CREATE TABLE IF NOT EXISTS member_notes (
    group_id     TEXT NOT NULL,
    sender_wxid  TEXT NOT NULL,
    notes_text   TEXT NOT NULL DEFAULT '',
    updated_at   REAL,
    PRIMARY KEY (group_id, sender_wxid)
);
CREATE TABLE IF NOT EXISTS group_notes (
    note_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id     TEXT NOT NULL,
    topic        TEXT,
    notes_text   TEXT NOT NULL,
    updated_at   REAL
);

-- ---------- Per-group member knowledge -----------------------------------
-- These tables are additive derived state.  messages remains the only raw
-- message store and is never copied into a member-specific table.
CREATE TABLE IF NOT EXISTS member_profiles (
    group_id             TEXT NOT NULL,
    sender_wxid          TEXT NOT NULL,
    display_name         TEXT,
    profile_json         TEXT NOT NULL DEFAULT '{}',
    summary_text         TEXT NOT NULL DEFAULT '',
    locked_sections_json TEXT NOT NULL DEFAULT '[]',
    version              INTEGER NOT NULL DEFAULT 1,
    created_at           REAL NOT NULL,
    updated_at           REAL NOT NULL,
    deleted_at           REAL,
    PRIMARY KEY (group_id, sender_wxid)
);
CREATE INDEX IF NOT EXISTS idx_member_profiles_group
    ON member_profiles(group_id, updated_at);

CREATE TABLE IF NOT EXISTS member_alias_history (
    group_id      TEXT NOT NULL,
    sender_wxid   TEXT NOT NULL,
    alias         TEXT NOT NULL,
    first_seen_at REAL NOT NULL,
    last_seen_at  REAL NOT NULL,
    seen_count    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (group_id, sender_wxid, alias)
);
CREATE INDEX IF NOT EXISTS idx_member_alias_history_lookup
    ON member_alias_history(group_id, alias);

CREATE TABLE IF NOT EXISTS member_claims (
    claim_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id      TEXT NOT NULL,
    sender_wxid   TEXT NOT NULL,
    section       TEXT NOT NULL CHECK(section IN (
        'identity','interests','skills','communication_style','habits',
        'relationships','opinions','sensitive_inferences','recent_focus'
    )),
    claim_text    TEXT NOT NULL,
    basis         TEXT NOT NULL CHECK(basis IN ('self_reported','observed','inferred')),
    confidence    REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
    sensitive     INTEGER NOT NULL DEFAULT 0 CHECK(sensitive IN (0,1)),
    status        TEXT NOT NULL DEFAULT 'current'
                  CHECK(status IN ('current','superseded','deleted')),
    superseded_by INTEGER,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    FOREIGN KEY (group_id, sender_wxid)
        REFERENCES member_profiles(group_id, sender_wxid) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_member_claims_member_status
    ON member_claims(group_id, sender_wxid, status, section);

CREATE TABLE IF NOT EXISTS member_claim_evidence (
    evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id   INTEGER NOT NULL,
    group_id   TEXT NOT NULL,
    sender_wxid TEXT NOT NULL,
    msg_id     INTEGER NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE (claim_id, msg_id),
    FOREIGN KEY (claim_id) REFERENCES member_claims(claim_id) ON DELETE CASCADE,
    FOREIGN KEY (msg_id) REFERENCES messages(msg_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_member_claim_evidence_msg
    ON member_claim_evidence(group_id, sender_wxid, msg_id);

CREATE TABLE IF NOT EXISTS member_update_state (
    group_id              TEXT NOT NULL,
    sender_wxid           TEXT NOT NULL,
    cursor_msg_id         INTEGER NOT NULL DEFAULT 0,
    cursor_t              INTEGER,
    full_history_complete INTEGER NOT NULL DEFAULT 0 CHECK(full_history_complete IN (0,1)),
    last_status           TEXT NOT NULL DEFAULT 'idle',
    last_error            TEXT,
    last_run_id           INTEGER,
    updated_at            REAL NOT NULL,
    PRIMARY KEY (group_id, sender_wxid),
    FOREIGN KEY (group_id, sender_wxid)
        REFERENCES member_profiles(group_id, sender_wxid) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_member_update_state_due
    ON member_update_state(group_id, updated_at);

CREATE TABLE IF NOT EXISTS member_update_runs (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id      TEXT NOT NULL,
    sender_wxid   TEXT NOT NULL,
    mode          TEXT NOT NULL CHECK(mode IN ('full','incremental','manual')),
    status        TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','skipped')),
    cursor_before INTEGER,
    cursor_after  INTEGER,
    chunk_count   INTEGER NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    error_text    TEXT,
    started_at    REAL NOT NULL,
    finished_at   REAL,
    details_json  TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_member_update_runs_member
    ON member_update_runs(group_id, sender_wxid, started_at);

-- Schema version, for future migrations. Bumped manually when DDL changes.
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('version', '5');
UPDATE schema_meta SET value = '5' WHERE key = 'version';

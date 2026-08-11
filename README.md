# WeChat Oracle

Local-first WeChat group archive and agent assistant.

[简体中文](README.zh-CN.md)

WeChat Oracle records selected WeChat group messages through an explicitly authorized local reader, [WeFlow](https://github.com/hicccc77/WeFlow), or visible UI fallback; stores them in SQLite; and uses an OpenAI-compatible model for replies and scheduled summaries. It can also OCR images, transcribe voice locally, and maintain long-term per-group memory.

The project is designed for a personal Windows machine running WeChat PC. Message data, media, memory, and debug logs stay under `data/` unless you explicitly send content to an LLM or vision provider.

## What It Does

- Ingests live WeChat messages from WeFlow SSE into SQLite.
- With explicit consent, discovers local WeChat groups and incrementally imports only the selected canonical groups into SQLite.
- Imports historical WeFlow JSON exports.
- Stores normalized messages, forwarded-message children, media paths, OCR/ASR transcripts, command runs, agent memory, and audit traces.
- Answers in group chat through wx4py UI automation.
- Supports slash commands such as `/find`, `/sum`, `/recent`, `/ask`, `/explain`, and `/balance`.
- Supports free-form agent chat when directly mentioned, replied to, or probability-triggered.
- Supports Local Ask from the terminal UI or CLI: ask the bot about one selected group without sending anything to WeChat.
- Runs a silent `lurk` learning path that updates `group_memory` / `persona_drift` without sending group messages.
- Can send detailed summaries for the previous completed hour and previous natural day on an idempotent schedule.
- Can use either the native in-process tool loop or an OpenClaw-backed runtime for agent turns.

## Architecture

```text
WeChat PC
   |
   v
WeFlow HTTP API
   |                         optional local worker
   | SSE live messages       OCR / ASR
   v                         |
ingest live -----------------+
   |
   v
SQLite WAL database: data/wechat-oracle.db
   |
   +--> dispatcher --> LLM / agent backend --> wx4py reply to WeChat
   |
   +--> local ask  --> LLM / agent backend --> terminal UI only
   |
   +--> agent lurk --> memory writes only, no reply
```

Production can be started with one supervisor command:

```powershell
uv run wechat-oracle run
```

`run` starts the selected ingest backend and `dispatcher` together. `WO_INGEST_BACKEND=weflow` uses the HTTP/SSE subscriber; `wx4py` reads text/link events exposed by the visible WeChat UI. `dispatcher` polls SQLite, processes explicit commands and agent triggers, and serializes all sends.

By default `run` opens a Textual terminal UI: the top area stays fixed with configuration, selected Local Ask group, and process status, while the rest of the screen scrolls child-process logs. Press `a` for a one-shot Local Ask, or `c` to configure the OpenAI-compatible endpoint/key/model, authorized local account/groups, hourly/daily schedules, and reply policy. The API key can be replaced but is never loaded back into the UI. Saving atomically updates `.env` and restarts affected processes. The initial UI uses the native SQLite/API path and does not require Pi Agent. Use `uv run wechat-oracle run --plain` to disable the TUI.

## Requirements

- Windows 10/11.
- WeChat PC 4.x, Qt version. UI Automation compatibility changes between releases and must be checked with `doctor` before enabling replies.
- A message source: the reviewed Weixin `4.1.11.55` local reader after explicit consent, WeFlow HTTP/SSE when an official functional build is available, or the `wx4py` visible-UI fallback. The local reader refuses an unreviewed Weixin build.
- Python 3.12+.
- [uv](https://docs.astral.sh/uv/).
- An OpenAI-compatible LLM endpoint, or OpenClaw local gateway for `WO_AGENT_BACKEND=openclaw`.

Non-reply workflows such as DB initialization, historical import, status checks, and WeFlow diagnostics can run without wx4py. Sending replies requires WeChat's main window to be visible, not minimized to tray.

## Quick Start

```powershell
git clone https://github.com/<your-account>/WeChat-Oracle.git
cd WeChat-Oracle
uv sync
uv run wechat-oracle setup
uv run wechat-oracle doctor
uv run wechat-oracle run
```

`setup` writes a minimal `.env`, `doctor` checks the database, selected ingest/agent backend, and reply path, and `run` starts ingest plus dispatcher with a small terminal UI. On Windows you can also double-click `scripts\run.bat` after setup.

## Windows Portable EXE

Build the console-mode, Windows x64 `onedir` package on Windows:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
```

The result is `dist\WeChatOracle\WeChatOracle.exe`. Keep and distribute the entire `dist\WeChatOracle` directory because the executable depends on its `_internal` directory. The first no-argument launch opens `setup` when `.env` is absent; later no-argument launches run the assistant. Explicit commands remain available:

```powershell
.\WeChatOracle.exe doctor
.\WeChatOracle.exe init-db
.\WeChatOracle.exe run
.\WeChatOracle.exe openclaw mcp-serve
```

The build includes the reviewed local-read implementation, but deliberately excludes `.env`, `data/`, personas, logs, raw/decrypted WeChat databases, database keys, and everything under `experimental/`. Keep the portable directory in a user-writable location rather than `Program Files`; runtime configuration and data live beside the executable. The speech model is downloaded to the user's cache on first ASR use.

`wx4py` is licensed under AGPL-3.0-or-later. The build copies its license and `THIRD_PARTY_NOTICES.md` into the output. A local personal build can be tested here, but do not redistribute it until the applicable source-distribution and other license obligations have been reviewed.

When using `WO_INGEST_BACKEND=weflow`, start an official functional WeFlow build and enable its HTTP API first. With `wx4py`, keep WeChat unlocked and visible; only UI-exposed text/link rows are archived, sender identity is unavailable, and downtime cannot be reconstructed completely.

Before enabling sends, test one exact group without sending anything:

```powershell
uv run wechat-oracle ingest ui-probe "人心黄黄"
```

For manual setup, create `.env` in the repository root. Start with the common settings:

```env
# Local archive. Keep disabled until the in-app setup has shown the account and groups.
WO_RAW_WECHAT_ENABLED=False
WO_RAW_WECHAT_ACCOUNT=
WO_RAW_WECHAT_SYNC_INTERVAL_SECONDS=60
WO_GROUPS=[]

# Bot identity in the target group
WO_BOT_NAME=<bot-group-nickname>
# Optional. Set manually if quote-replying to the bot does not trigger.
# WO_BOT_WXID=wxid_xxx

# Reply path
WO_REPLY=True
WO_REPLY_BACKEND=uia-direct
WO_REPLY_MENTION_POLICY=explicit
WO_REPLY_ALLOWED_GROUPS=人心黄黄
WO_REPLY_FAIL_CLOSED=True

# Previous completed hour and previous natural day. Both are opt-in.
WO_HOURLY_SUMMARY_ENABLED=False
WO_DAILY_SUMMARY_ENABLED=False
WO_SUMMARY_TIMEZONE=Asia/Hong_Kong
WO_SUMMARY_SYNC_GRACE_SECONDS=300

# Optional agent tuning
WO_AGENT_BASE_PROBABILITY=0.25
WO_AGENT_PROACTIVE_MODE=reactive
WO_AGENT_CONTINUATION_ENABLED=True
WO_AGENT_CONTINUATION_MAX_FOLLOWUPS=2
WO_AGENT_CONTINUATION_DELAY_SECONDS=90
WO_AGENT_CONTINUATION_TTL_SECONDS=600
WO_AGENT_RECENT_CONTEXT_CHAT=100
WO_LLM_MAX_TOKENS=5000
WO_LLM_WRITE_MAX_TOKENS=10000

# Optional image reading
WO_VISION_API_KEY=
WO_VISION_ENDPOINT=https://dashscope.aliyuncs.com/compatible-mode/v1
WO_VISION_MODEL=qwen-vl-plus
WO_VISION_MAX_IMAGES=3
WO_VISION_MAX_TOKENS=800

# Optional background learning
WO_AGENT_LURK_ENABLED=False
WO_AGENT_LURK_INTERVAL_SECONDS=1800
WO_AGENT_LURK_MIN_NEW_MESSAGES=20
```

`WO_BOT_NAME` and `WO_BOT_WXID` identify different things. `WO_BOT_NAME` is the nickname used to detect real `@<bot>` mentions in the group. `WO_BOT_WXID` is the bot account's own wxid and is only needed for the reply-to-bot trigger. The dispatcher auto-discovers it from stored bot messages when possible; set it manually if quote-replying to the bot does not wake the agent.

The initial portable configuration uses the built-in SQLite memory and native OpenAI-compatible client:

```env
WO_AGENT_BACKEND=native
WO_LLM_API_KEY=<api-key>
WO_LLM_ENDPOINT=https://api.deepseek.com
WO_LLM_MODEL=deepseek-v4-pro
```

The source tree retains OpenClaw as an advanced compatibility path, but it is not part of the initial setup flow. If needed, configure it manually:

```env
WO_AGENT_BACKEND=openclaw
WO_OPENCLAW_GATEWAY_URL=http://127.0.0.1:18789
WO_OPENCLAW_TOKEN=<gateway-token>
WO_OPENCLAW_AGENT_ID=<your-agent-id>
WO_OPENCLAW_TIMEOUT_SECONDS=300
```

Pi Agent is not bundled, configured, or required by the initial portable build.

Then either run both processes through the supervisor:

```powershell
uv run wechat-oracle run
```

Or run them manually in two terminals:

```powershell
# Terminal 1: live ingest + OCR/ASR worker
uv run wechat-oracle ingest live

# Terminal 2: command/agent dispatcher
uv run wechat-oracle dispatcher
```

In a watched group:

```text
@<bot-name> /help
@<bot-name> Who mentioned stocks today?
@<bot-name> /find since:2026-05 about home renovation
```

If `WO_GROUPS` is empty, live ingest monitors every group chat currently exposed by WeFlow sessions. You can also set `WO_GROUPS` to a comma-separated list of group names, remarks, or wxids.

## Core Commands

General CLI:

```powershell
uv run wechat-oracle setup
uv run wechat-oracle doctor
uv run wechat-oracle run
uv run wechat-oracle init-db
uv run wechat-oracle status
uv run wechat-oracle ingest live
uv run wechat-oracle ingest backfill <export.json> --format weflow
uv run wechat-oracle dispatcher
uv run wechat-oracle worker mm
```

WeFlow diagnostics:

```powershell
uv run wechat-oracle weflow find <group-name-or-wxid-fragment>
uv run wechat-oracle weflow sessions --groups-only
```

Agent state:

```powershell
uv run wechat-oracle agent show <group_id>
uv run wechat-oracle agent show-runs <group_id> -n 10
uv run wechat-oracle agent ask <group_id-or-name> summarize today
uv run wechat-oracle agent ask <group_id-or-name> update memory from this topic --write
uv run wechat-oracle agent lurk <group_id>
uv run wechat-oracle agent wipe <group_id>
uv run wechat-oracle agent wipe <group_id> --persona-only
uv run wechat-oracle agent wipe <group_id> --memory-only -y
```

Health checks:

```powershell
uv run wechat-oracle verify roundtrip
```

OpenClaw runtime helpers:

```powershell
uv run wechat-oracle openclaw ping
uv run wechat-oracle openclaw mcp-test
uv run wechat-oracle openclaw mcp-serve
```

`openclaw mcp-serve` is a stdio MCP server entrypoint intended to be spawned by OpenClaw. `openclaw ping` tests the configured OpenClaw chat-completions gateway.

Convenience wrappers:

```powershell
scripts\run.bat
scripts\track.bat
scripts\import.bat <export.json>
```

`scripts\run.bat` starts `wechat-oracle run`. `scripts\track.bat` is the lower-level live-ingest-only wrapper kept for debugging.

## Historical Backfill

Backfill imports historical WeFlow exports into the same SQLite archive used by live ingest:

```powershell
uv run wechat-oracle ingest backfill <export.json> --format weflow
uv run wechat-oracle ingest backfill <weflow-export-dir> --format weflow
```

The command imports one export file, or all JSON files under a WeFlow export directory's `texts/` folder. Re-running the same file or directory is safe: all imported messages go through `ingest/writer.py:write_messages()`, and `UNIQUE(dedupe_key)` skips duplicates.

Supported formats:

| Format | Input |
|---|---|
| `weflow` | WeFlow JSON export with top-level `session` and `messages` fields, or a WeFlow export directory containing `texts/`. |
| `jsonl` | One canonical `Message` JSON object per line, mainly useful for pipeline tests or custom importers. |

For WeFlow exports, the importer:

- Derives `group_id` from `session.wxid`, falling back to the filename stem.
- Derives `group_name` from `session.displayName`, `session.nickname`, or `session.remark`.
- Converts WeFlow `localType` values into normalized message types: text, image, voice, video, sticker, link, quote, forward, and system.
- Preserves quote replies using `reply_to_wx_msg_id` and `quote_text`.
- Parses merged-forward messages into `forwarded_records`, so `/find`, `/sum`, and agent history tools can search inside forwarded bundles. Forwarded child media gets its own `media_path` when WeFlow/export XML exposes a local file, or when `src_msg_id` matches an already archived media message.
- Copies referenced local media files from the export folder into `data/media/<group_id>/<kind>/` and stores a `data/`-relative `media_path`.

If a media file referenced by the export is missing, the message is still imported with a missing-media placeholder. OCR/ASR can only run for media files that exist under `data/media`.

Backfilled rows use `source='backfill'`. The dispatcher only wakes on new `source='live'` rows, so importing old messages will not make the bot reply to historical mentions. Backfilled messages are still available to `/find`, `/sum`, `/recent`, agent history search, and manual or automatic `lurk` learning.

### Authorized Local WeChat Archive

For the exact signed Windows Weixin `4.1.11.55` build reviewed by this project, the opt-in local reader can discover accounts and groups, copy stable WCDB message/contact snapshots, and incrementally import only canonical groups selected by the user. It is disabled until `WO_RAW_WECHAT_ENABLED=True`; the first-run UI performs the same account/group authorization flow as these CLI commands.

```powershell
# Sanitized inventory; no opt-in and no key scan.
uv run wechat-oracle raw scan

# After setting WO_RAW_WECHAT_ENABLED=True, list canonical groups.
$account = '<anonymous account fingerprint from scan>'
uv run wechat-oracle raw groups --account $account
uv run wechat-oracle raw authorize '<...@chatroom>' --account $account

# One incremental cycle, or continuous monitoring.
uv run wechat-oracle raw sync --account $account
uv run wechat-oracle raw run --account $account

# Inspect or revoke persisted authorization.
uv run wechat-oracle raw status
uv run wechat-oracle raw revoke '<...@chatroom>' --account $account
```

The reader refuses unrecognized executable/module hashes. It discovers every numeric `message_0.db` ... `message_N.db` shard, opens Weixin processes with read/query permissions only, accepts a candidate key only after checking database-page authentication, keeps the raw key in memory, and emits only anonymous fingerprints and counts. Continuous sync decrypts only changed shards and never advances a cursor before normalized archive writes commit.

Source databases are never modified. A database and its WAL must remain unchanged across a complete copy attempt before the snapshot is accepted; transient SHM state is not copied. WAL header/frame checksums, salts, every database-page HMAC, and the last commit marker are validated before committed frames are applied. Temporary snapshots stay under git-ignored `data/raw_wechat/`; full decrypted copies are removed after selected messages are normalized, and every snapshot must pass SQLite `quick_check` before use.

Authorization persists the exact account fingerprint, canonical `@chatroom` id, display name, and contact-database generation. A contact-generation change pauses that group until it is selected again. Imports include `local_type=1` text rows, pass through the normal deduplicating writer with `source='backfill'`, and the raw module has no reply/send capability. The canonical id also joins raw history with UI live context and prevents duplicate scheduled summaries. The profile is build-specific and must not be reused after a Weixin update until the new binary and database format are separately reviewed.

Scheduled summaries run only for groups that have both a persisted canonical authorization and an exact display-name send allowlist. Hourly output starts with `#过去一小时话题`; daily output starts with `#过去一天话题`. Generation and send claims are crash-safe and idempotent. If the process loses certainty after submitting a UI send, that delivery is marked `unknown` and is never retried automatically, which favors avoiding duplicate group posts.

## In-Group Commands

The dispatcher accepts explicit slash commands either as `@<bot> /cmd ...` or as a standalone `/cmd ...` group message.

| Command | Purpose |
|---|---|
| `/find [from:<person>\|@<person>] [since:YYYY[-MM[-DD]]] <query>` | Semantic search over the current group archive. |
| `/sum [from:<person>\|@<person>] [since:date] [until:date] [limit:N] [topic]` | Hierarchically summarize an uncapped current-group period. |
| `/recent [N]` | Show recent ingested messages without calling an LLM. |
| `/ask <question>` | Lightweight LLM call with no group-history context. |
| `/explain [text]` | Explain quoted message or supplied text. |
| `/balance` | Query the configured DeepSeek-compatible balance endpoint. |
| `/help [command]` | Show command help. |

Examples:

```text
@Assistant /find from:Alice since:2026-05 about home renovation
@Assistant /sum limit:100 what did people discuss last night?
@Assistant 总结一下昨天聊了什么
@Assistant 总结最近3小时的装修讨论
@Assistant /recent 20
@Assistant /ask What is SQLite WAL?
Reply to an image and send: @Assistant /explain
```

## Agent Behavior

There are four separate runtime paths.

### 1. Collection Path

`ingest live`, `ingest backfill`, and `worker mm` only write normalized data to SQLite. They do not decide whether the bot should speak.

### 2. Interaction Path

The dispatcher classifies each new live message:

| Trigger | Condition | Behavior |
|---|---|---|
| `mention` | Text contains a real `@<WO_BOT_NAME>` mention. | Always wakes the bot. Slash commands are parsed; bare text enters agent chat. |
| `reply` | Message quote-replies to a prior bot message, and `WO_BOT_WXID` is known or auto-discovered. | Always wakes the bot. |
| `probability` | Not mention/reply, `WO_AGENT_PROACTIVE_MODE` is not `off`, and the message passes type gate, random threshold, and cooldown. | Agent decides whether to speak or `stay_silent`, following the configured participation posture. |
| none | No trigger. | No LLM call; message is finalized as no-trigger. |

Direct mention and reply-to-bot triggers run the agent directly. The bot sends a group reply only if the agent returns a non-empty response.

`WO_REPLY_MENTION_POLICY` controls whether outgoing group replies notify a member:

- `always`: every group reply attempts a real `@requester`.
- `explicit`: the default. Direct mentions, reply-to-bot turns, slash commands, and command errors `@` the requester; `probability` / `proactive` ambient replies do not.
- `never`: send plain group messages without `@`.

`WO_AGENT_PROACTIVE_MODE` controls only non-explicit probability wakeups:

- `off`: never wake from ambient chat; only direct mentions, reply-to-bot, and slash commands run the agent.
- `reactive`: the default. A probability wakeup may join the current topic only when it has useful context.
- `proactive`: a probability wakeup may also ask one short context-based question, connect an old thread, or start a light related topic when the timing is appropriate.

Continuation follow-ups are separate from probability. When `WO_AGENT_CONTINUATION_ENABLED=True`, the agent may call `schedule_followup` while replying. The system stores only an intent and reruns the agent later with fresh context:

- `committed`: the bot explicitly promised to come back, so the follow-up can run even if nobody speaks meanwhile.
- `thread`: the bot may continue only if new non-bot messages keep the same topic moving; otherwise the job is cancelled.

Follow-ups use the same per-group dispatcher queue and serialized wx4py sender as normal replies. They respect `WO_AGENT_CONTINUATION_MAX_FOLLOWUPS`, `WO_AGENT_CONTINUATION_DELAY_SECONDS`, `WO_AGENT_CONTINUATION_TTL_SECONDS`, and `WO_REPLY_MENTION_POLICY`.

The native agent has two phases:

- Phase A reads recent context, may call read-only tools, and returns either a chat reply or `stay_silent`.
- Phase B sees the Phase A trace and may update `group_memory` or `persona_drift`.

Phase A initial context includes the latest `WO_AGENT_RECENT_CONTEXT_CHAT` messages from the current group. Tools can search older history, expand quote chains, expand forwarded bundles, read OCR/ASR text, read images through a vision model, read voice transcripts, or read group memory.

### 3. Local Ask Path

The Textual UI and CLI can run an agent turn against a selected group without sending anything to WeChat.

```powershell
uv run wechat-oracle agent ask <group_id-or-name> summarize today's discussion
uv run wechat-oracle agent ask <group_id-or-name> summarize Alice in August 2024 and update memory --write
```

In the TUI, use keyboard shortcuts:

```text
g  open group picker
a  open Local Ask dialog for the selected group
m  edit group_memory / persona_drift for the selected group
c  edit agent backend / native model / OpenClaw agent id / probability wakeup / @ policy, then restart dispatcher
w  toggle read-only/write mode
```

Local Ask defaults to read-only: it may read recent context, search history, inspect media, and read group memory, but it will not update `group_memory` / `persona_drift`. Use `--write` in the CLI or press `w` in the TUI to run a `local_task` that may update memory. Local Ask writes an `agent_run_log` audit row with `trigger_kind='local_ask'` or `trigger_kind='local_task'`; it never writes `command_runs` and never calls wx4py.

### 4. Background Learning Path

`agent lurk <group_id>` and optional auto-lurk read a watermarked batch of new messages and update memory without replying.

- First run reads the latest `WO_AGENT_LURK_RECENT_MSGS` messages.
- Later runs continue from `agent_lurk_state.last_msg_id`.
- The lurk agent may call history tools to inspect older messages when the new batch points to prior context.
- It writes only stable, reusable knowledge to `group_memory` or long-term behavior adjustments to `persona_drift`.
- It never calls wx4py and never sends an acknowledgement.

Enable automatic lurk inside dispatcher with:

```env
WO_AGENT_LURK_ENABLED=True
WO_AGENT_LURK_INTERVAL_SECONDS=1800
WO_AGENT_LURK_MIN_NEW_MESSAGES=20
```

Auto-lurk uses a separate single worker, so it does not occupy chat response workers.

## Agent Backends

`WO_AGENT_BACKEND=native` is the default. It runs the tool-calling loop inside this process through your OpenAI-compatible LLM endpoint.

`WO_AGENT_BACKEND=openclaw` delegates chat and lurk turns to an OpenClaw agent through a local OpenAI-compatible gateway. OpenClaw can use the project's MCP server to call WeChat-Oracle tools. This is useful if your production bot should use an existing subscription-backed agent runtime.

Switching between the two modes is controlled by one setting:

```env
# Use the ordinary OpenAI-compatible API directly.
WO_AGENT_BACKEND=native

# Or use OpenClaw as the agent runtime.
WO_AGENT_BACKEND=openclaw
```

Important distinction: OpenClaw here is an agent runtime backend, not a WeChat reply backend. Group replies still go through `uia-direct`, wx4py, or `stdout`.

OpenClaw-related settings:

```env
WO_AGENT_BACKEND=openclaw
WO_OPENCLAW_GATEWAY_URL=http://127.0.0.1:18789
WO_OPENCLAW_TOKEN=<gateway-token>
WO_OPENCLAW_AGENT_ID=<your-agent-id>
WO_OPENCLAW_TIMEOUT_SECONDS=300
```

## Real WeChat Mentions

`UiaDirectReplier` is the preferred WeChat 4.x sender when mouse simulation is undesirable. It passively attaches to the existing WeChat window (without wx4py's registry repair/restart path), opens one exact session or unique exact search result through UI Automation accessibility actions, verifies that the same session is selected, focuses the chat edit, writes through ValuePattern (clipboard plus focused keyboard input only as a fallback), and submits with Enter. It never calls `Click`, `DoubleClick`, or wx4py's public `send_to()` path. Windows must still be unlocked and WeChat's main window must exist.

Both `UiaDirectReplier` and `Wx4pyReplier` create a real WeChat group mention token by opening the group, typing `@`, selecting the requester from WeChat's candidate popup, then placing the reply body. If the candidate cannot be detected and verified, the send is refused; neither backend silently falls back to literal `@name` text.

This path is used only when `WO_REPLY_MENTION_POLICY` says the current reply should mention someone.

This is intentionally serialized through `_SerialReplier`; multiple worker threads never manipulate the WeChat GUI at the same time.

## Multimedia

The mm worker fills `messages.transcript`:

- Images and stickers: local OCR through `rapidocr-onnxruntime`.
- Voice: local ASR through `faster-whisper`.

Transcript states:

- `NULL`: not processed yet.
- `''`: processed but no text recognized.
- non-empty text: usable OCR/ASR result.

If `WO_VISION_API_KEY` is configured, agent chat and `/explain` / `/ask` can send referenced images to a vision model for direct reading. Without it, native image handling falls back to OCR text and placeholders. In OpenClaw mode, MCP also exposes `load_image`, which returns the original image block for a vision-capable OpenClaw agent to inspect directly; `read_image` still means "return a textual vision-model reading".

## Data Model

For the full ingestion contract, including WeFlow `localType` handling, app-message subtypes, media storage, quote/forward normalization, and LLM-visible rendering, see [docs/message-model.md](docs/message-model.md).

Key tables:

| Table | Purpose |
|---|---|
| `messages` | Normalized WeChat messages. |
| `group_aliases` | Maps display-name UI ids onto verified canonical `@chatroom` ids. |
| `forwarded_records` | Children inside merged-forward messages. |
| `command_runs` | Dispatcher idempotency and command status. |
| `group_state` | Live/backfill per-group cursors. |
| `persona_drift` | Per-group evolvable behavior supplement. |
| `group_memory` | Per-group freeform long-term memory document. |
| `agent_run_log` | Agent audit traces for chat, lurk, and Local Ask turns. |
| `agent_lurk_state` | Lurk cursor, separate from audit logs. |
| `agent_proactive_outbox` | Delayed proactive continuation jobs. Stores intent, not pre-generated reply text. |

All write paths for imported messages go through `ingest/writer.py:write_messages()` and rely on `UNIQUE(dedupe_key)` for cross-source deduplication.

`group_memory` and `persona_drift` are replace-on-write. Write tools force the agent to read current state before writing, and they check that state did not change in between. If another concurrent agent run updated memory first, the tool asks the model to re-read and merge.

## Configuration Reference

All runtime settings use the `WO_` prefix and can be set in `.env` or the process environment.

| Variable | Default | Notes |
|---|---:|---|
| `WO_DATA_DIR` | `data` | Data root. |
| `WO_DB_PATH` | `data/wechat-oracle.db` | SQLite database path. |
| `WO_MEDIA_DIR` | `data/media` | Media directory. |
| `WO_GROUPS` | `[]` | Empty means all WeFlow group sessions; comma string or JSON list is accepted. |
| `WO_RAW_WECHAT_ENABLED` | `False` | Enables the reviewed local WeChat reader after explicit consent. |
| `WO_RAW_WECHAT_ACCOUNT` | empty | Exact anonymous account fingerprint selected by the user. |
| `WO_RAW_WECHAT_WORKSPACE` | `data/raw_wechat` | Git-ignored staging/state directory for stable snapshots. |
| `WO_RAW_WECHAT_INSTALL_ROOT` | `D:\0softwear\Weixin` | Supported Weixin install root; running-process discovery is also attempted. |
| `WO_RAW_WECHAT_SYNC_INTERVAL_SECONDS` | `60` | Continuous local archive poll interval; minimum 30 seconds. |
| `WO_LOG_LEVEL` | `INFO` | loguru level. |
| `WO_WX4PY_LOG_LEVEL` | `WARNING` | Python logging level for wx4py internals; set to `INFO` only when debugging UI automation. |
| `WO_WEFLOW_BASE_URL` | `http://127.0.0.1:5031` | WeFlow HTTP API root. |
| `WO_WEFLOW_TOKEN` | empty | WeFlow token. |
| `WO_INGEST_BACKEND` | `weflow` | `weflow` or visible-UI `wx4py`. |
| `WO_BOT_NAME` | empty | Bot's group nickname. Required by dispatcher. |
| `WO_BOT_WXID` | empty | Optional bot wxid for reply-to-bot trigger. |
| `WO_LLM_PROVIDER` | `openai-compatible` | LLM provider adapter. |
| `WO_LLM_API_KEY` | empty | LLM API key. |
| `WO_LLM_ENDPOINT` | `https://api.deepseek.com` | OpenAI-compatible endpoint. |
| `WO_LLM_MODEL` | `deepseek-v4-pro` | Model name. |
| `WO_LLM_JSON_MODE` | `native` | `native` or `prompt` JSON mode. |
| `WO_DISPATCHER_POLL_INTERVAL` | `3.0` | DB polling interval in seconds. |
| `WO_DISPATCHER_WORKER_THREADS` | `4` | Global message workers. Messages are serialized per group but different groups can run in parallel; wx4py sends remain serialized. |
| `WO_DISPATCHER_CANDIDATE_LIMIT` | `500` | `/find` candidate cap. |
| `WO_DISPATCHER_CONTEXT_CHAT` | `2500` | Legacy chat context cap, still used by some summary paths. |
| `WO_HOURLY_SUMMARY_ENABLED` | `False` | Idempotently summarize the previous completed clock hour after the grace period. |
| `WO_HOURLY_SUMMARY_MIN_MESSAGES` | `5` | Skip an hourly summary below this effective-message count. |
| `WO_DAILY_SUMMARY_ENABLED` | `False` | At startup/midnight, idempotently summarize the previous Asia/Hong_Kong natural day. |
| `WO_DAILY_SUMMARY_MIN_MESSAGES` | `5` | Skip automatic summary below this effective-message count. |
| `WO_DAILY_SUMMARY_CHUNK_CHARS` | `800` | Maximum characters per outbound summary part. |
| `WO_DAILY_SUMMARY_SEND_DELAY_SECONDS` | `1.2` | Delay between outbound summary parts. |
| `WO_SUMMARY_TIMEZONE` | `Asia/Hong_Kong` | IANA timezone used for hourly and natural-day boundaries. |
| `WO_SUMMARY_SYNC_GRACE_SECONDS` | `300` | Wait after a period closes so local DB sync can catch up. |
| `WO_SUMMARY_GENERATION_LEASE_SECONDS` | `900` | Crash-recovery lease for summary generation. |
| `WO_SUMMARY_SENDING_LEASE_SECONDS` | `300` | After this sending lease expires, delivery becomes unknown and is never auto-retried. |
| `WO_LLM_MAX_TOKENS` | `5000` | General output cap. |
| `WO_LLM_CHAT_MAX_TOKENS` | empty | Overrides chat cap. |
| `WO_LLM_SUM_MAX_TOKENS` | empty | Overrides summary cap. |
| `WO_LLM_SHORT_MAX_TOKENS` | empty | Overrides `/ask` / `/explain` cap. |
| `WO_LLM_WRITE_MAX_TOKENS` | `10000` | Cap for memory-write tool calls. |
| `WO_VISION_PROVIDER` | `openai-compatible` | Vision adapter. |
| `WO_VISION_API_KEY` | empty | Enables direct image reading. |
| `WO_VISION_ENDPOINT` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | Vision endpoint. |
| `WO_VISION_MODEL` | `qwen-vl-plus` | Vision model. |
| `WO_VISION_MAX_IMAGES` | `3` | Per vision request image cap. |
| `WO_VISION_MAX_TOKENS` | `800` | Vision output cap. |
| `WO_AGENT_BASE_PROBABILITY` | `0.25` | Per-message probability wake threshold for eligible ambient messages. |
| `WO_AGENT_PROACTIVE_MODE` | `reactive` | `off` disables ambient probability wakeups; `reactive` only joins the current topic; `proactive` may ask a short context-based question or start a light related topic. |
| `WO_AGENT_CONTINUATION_ENABLED` | `True` | Allows the agent to explicitly schedule delayed follow-ups by intent. |
| `WO_AGENT_CONTINUATION_MAX_FOLLOWUPS` | `2` | Max follow-up messages after the source reply. |
| `WO_AGENT_CONTINUATION_DELAY_SECONDS` | `90` | Default delay before reevaluating a scheduled follow-up. |
| `WO_AGENT_CONTINUATION_TTL_SECONDS` | `600` | Expiry window for pending follow-ups. |
| `WO_AGENT_COOLDOWN_SECONDS` | `30` | Per-group probability cooldown. |
| `WO_AGENT_MAX_STEPS` | `8` | Native Phase A max rounds. |
| `WO_AGENT_REFLECT_MAX_STEPS` | `3` | Native Phase B max rounds. |
| `WO_AGENT_REFLECTION_ENABLED` | `True` | Enables chat Phase B memory reflection. |
| `WO_AGENT_PERSONAS_DIR` | `data/personas` | Persona YAML directory. |
| `WO_AGENT_RECENT_CONTEXT_CHAT` | `100` | Initial recent-message window for agent chat. |
| `WO_AGENT_MEMORY_MAX_CHARS` | `100000` | Hard cap for `group_memory`. |
| `WO_AGENT_MAX_TOOL_CALLS_PER_RUN` | `20` | Native Phase A tool budget. |
| `WO_AGENT_MAX_TOOL_CALLS_PER_STEP` | `4` | Native Phase A per-step tool budget. |
| `WO_AGENT_MAX_IMAGE_READS_PER_RUN` | `2` | Native image-read budget. |
| `WO_AGENT_MAX_VOICE_READS_PER_RUN` | `2` | Native voice-read budget. |
| `WO_AGENT_LURK_ENABLED` | `False` | Enables dispatcher auto-lurk. |
| `WO_AGENT_LURK_INTERVAL_SECONDS` | `1800` | Auto-lurk scan interval. |
| `WO_AGENT_LURK_MIN_NEW_MESSAGES` | `20` | Minimum new messages before auto-lurk. |
| `WO_AGENT_LURK_RECENT_MSGS` | `100` | Max messages processed in one lurk run. |
| `WO_AGENT_LURK_MAX_STEPS` | `4` | Lurk tool-calling rounds. |
| `WO_OPENCLAW_GATEWAY_URL` | `http://127.0.0.1:18789` | OpenClaw gateway. |
| `WO_OPENCLAW_TOKEN` | empty | OpenClaw gateway token. |
| `WO_OPENCLAW_AGENT_ID` | `wechat-bot` | OpenClaw agent id. |
| `WO_OPENCLAW_TIMEOUT_SECONDS` | `300` | OpenClaw gateway request timeout. |
| `WO_PI_EXECUTABLE` | `pi` | Pi CLI executable. |
| `WO_PI_PROVIDER` | `opencode-go` | Pi provider; credentials remain owned by Pi. |
| `WO_PI_MODEL` | `deepseek-v4-flash` | Pi model. |
| `WO_PI_THINKING` | `low` | Pi thinking level. |
| `WO_PI_TIMEOUT_SECONDS` | `300` | Per-call Pi RPC timeout. |
| `WO_AGENT_BACKEND` | `native` | `native`, `openclaw`, or `pi`. |
| `WO_REPLY` | `True` | Send replies back to WeChat. |
| `WO_REPLY_BACKEND` | `uia-direct` | `uia-direct` (no mouse), `wx4py` (ordinary visible UI automation), or `stdout`. |
| `WO_REPLY_MENTION_POLICY` | `explicit` | `always` mentions the requester on every group reply; `explicit` mentions only direct/command/reply triggers; `never` sends plain group messages. |
| `WO_REPLY_ALLOWED_GROUPS` | `[]` | Exact display names allowed for UI sends; empty blocks both UI backends. |
| `WO_REPLY_FAIL_CLOSED` | `True` | Refuse startup/send instead of silently degrading after wx4py failure. |

`WO_WHISPER_MODEL` is read directly by the mm worker and defaults to `small`; accepted values depend on faster-whisper, commonly `tiny`, `base`, `small`, `medium`, and `large-v3`.

## Logs and Debugging

- `data/dispatcher.log`: compact human-readable command and agent traces.
- `data/llm_debug.log`: LLM prompts, raw replies, parsed results, and full traces.
- `data/events.jsonl`: lightweight machine-readable lifecycle events for ingest batches, trigger decisions, command/agent runs, native tool calls, memory writes, and reply attempts. Use this to answer operational questions such as "why did this message trigger?" or "where did the latency go?" without reading full prompts.
- `data/*.process.log`: rotating loguru process logs for long-running processes such as `live` and `dispatcher`.
- `data/openclaw.log` / `data/mcp.log`: JSONL audit logs for OpenClaw gateway calls and MCP tool invocations.
- `wx4py_send_audit.jsonl`: wx4py send audit records when wx4py emits them.

Useful checks:

```powershell
uv run wechat-oracle status
uv run wechat-oracle verify roundtrip
uv run wechat-oracle agent show-runs <group_id> -n 20
```

`verify roundtrip` confirms whether WeFlow echoes the bot's own wx4py-sent messages back into `messages`. That echo is required for bot wxid auto-discovery and reply-to-bot triggers.

## Known Limits

- wx4py depends on a visible Windows desktop and WeChat Qt UI. It is not an official WeChat API.
- Group reply delivery is implemented only through `wx4py` or local `stdout`.
- OpenClaw is supported as an agent runtime backend, not as a group-message reply backend.
- If WeFlow does not expose historical media files, old images/voices imported by backfill cannot be OCRed/transcribed until you provide media.
- Video content is stored as metadata/path only; video understanding is not implemented.
- Deduplication is best-effort when WeFlow lacks `wx_msg_id`.

## Development

The repository has a pytest suite. Use the local checks below before committing:

```powershell
uv run pytest -q
uv run python -m compileall src\wechat_oracle
uv run python -m compileall experimental\raw_wechat
uv run wechat-oracle agent --help
uv run wechat-oracle status
uv run python .claude\hooks\check_doc_sync.py
git diff --check
```

Enable the git hook once per clone:

```powershell
git config core.hooksPath .githooks
```

When changing schema, CLI commands, config fields, or dispatcher command syntax, update this README in the same commit.

## License

MIT. See [LICENSE](LICENSE).

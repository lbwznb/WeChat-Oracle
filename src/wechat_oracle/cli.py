"""Typer CLI entry point: `wechat-oracle <subcommand>`.

Production entry points include long-running processes, one-shot imports, and diagnostics:
  - `setup`             - interactive first-run .env writer
  - `doctor`            - local readiness checks
  - `run`               - start live ingest + dispatcher together
  - `init-db`           - create schema (idempotent)
  - `ingest backfill`   - one-shot import of historical export files
  - `ingest live`       - long-running SSE subscriber -> DB writer
  - `dispatcher`        - long-running DB poller -> LLM -> wx4py reply
  - `status`            - quick row count health check

Plus `weflow find` / `weflow sessions` for diagnosing WO_GROUPS resolution.

Adding a subcommand: also update README quickstart + process table
(CLAUDE.md F5; the doc-sync hook will remind you).
"""
from pathlib import Path
from collections import deque
from datetime import datetime
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time

import httpx
import typer
from loguru import logger

from .config import settings
from .db import get_conn, init_db, transaction
from .ingest.backfill import import_file
from .ingest.writer import write_messages

app = typer.Typer(no_args_is_help=True, add_completion=False)
ingest_app = typer.Typer(no_args_is_help=True)
weflow_app = typer.Typer(no_args_is_help=True, help="Inspect what WeFlow's HTTP API exposes (diagnose WO_GROUPS issues, etc.)")
worker_app = typer.Typer(no_args_is_help=True, help="Background workers that fill in derived data on messages rows.")
verify_app = typer.Typer(no_args_is_help=True, help="Health checks for the dispatch pipeline.")
agent_app = typer.Typer(no_args_is_help=True, help="Inspect & manage agent memory (persona_drift / group_memory / run logs).")
openclaw_app = typer.Typer(no_args_is_help=True, help="OpenClaw runtime backend (the recommended agent path; uses subscription instead of per-token API).")
raw_app = typer.Typer(no_args_is_help=True, help="Authorized read-only local WeChat database synchronization.")
member_kb_app = typer.Typer(no_args_is_help=True, help="Inspect and maintain evidence-linked per-member knowledge.")
app.add_typer(ingest_app, name="ingest")
app.add_typer(weflow_app, name="weflow")
app.add_typer(worker_app, name="worker")
app.add_typer(verify_app, name="verify")
app.add_typer(agent_app, name="agent")
app.add_typer(openclaw_app, name="openclaw")
app.add_typer(raw_app, name="raw")
app.add_typer(member_kb_app, name="member-kb")


def _run_raw_command(command: str, *args: str) -> None:
    from .raw_wechat.cli import main as raw_main

    code = raw_main([command, *args])
    if code:
        raise typer.Exit(code)


@raw_app.command("scan")
def raw_scan() -> None:
    """Discover anonymous local WeChat accounts and numeric message shards."""
    _run_raw_command("scan")


@raw_app.command("groups")
def raw_groups(
    account: str = typer.Option("", "--account", help="Anonymous account fingerprint"),
) -> None:
    """Decrypt the current contact snapshot and list selectable chatrooms."""
    _run_raw_command("groups", *(["--account", account] if account else []))


@raw_app.command("authorize")
def raw_authorize(
    canonical_id: str = typer.Argument(..., help="Exact @chatroom id returned by raw groups"),
    account: str = typer.Option("", "--account", help="Anonymous account fingerprint"),
) -> None:
    """Authorize one exact canonical group for local archive reads."""
    args = ["--canonical-id", canonical_id]
    if account:
        args.extend(["--account", account])
    _run_raw_command("authorize", *args)


@raw_app.command("revoke")
def raw_revoke(
    canonical_id: str = typer.Argument(...),
    account: str = typer.Option("", "--account"),
) -> None:
    """Disable future reads for one previously authorized group."""
    args = ["--canonical-id", canonical_id]
    if account:
        args.extend(["--account", account])
    _run_raw_command("revoke", *args)


@raw_app.command("sync")
def raw_sync(account: str = typer.Option("", "--account")) -> None:
    """Run one incremental synchronization for all authorized groups."""
    _run_raw_command("sync", *(["--account", account] if account else []))


@raw_app.command("run")
def raw_run(account: str = typer.Option("", "--account")) -> None:
    """Continuously synchronize all authorized groups."""
    _run_raw_command("run", *(["--account", account] if account else []))


@raw_app.command("status")
def raw_status() -> None:
    """Show sanitized local WeChat authorization and process status."""
    _run_raw_command("status")


def _member_kb_llm():
    from .llm import build_llm_client

    return build_llm_client(
        provider=settings.llm_provider,
        api_key=settings.llm_api_key,
        endpoint=settings.llm_endpoint,
        json_mode=settings.llm_json_mode,
    )


def _resolve_member_selector(conn, group_id: str, selector: str) -> str:
    from .member_knowledge import list_member_profiles

    exact: list[str] = []
    # Exact stable ids remain resolvable even after the derived profile was
    # deleted, so `rebuild --member <wxid>` can revive it from raw history.
    raw = conn.execute(
        """
        SELECT DISTINCT CASE
            WHEN sender_wxid IS NULL OR TRIM(sender_wxid)='' THEN '__unknown__'
            ELSE TRIM(sender_wxid) END AS member
          FROM messages
         WHERE group_id=?
        """,
        (group_id,),
    ).fetchall()
    exact.extend(str(row["member"]) for row in raw if selector == str(row["member"]))
    for item in list_member_profiles(conn, group_id):
        sender = str(item.get("sender_wxid") or "")
        names = {
            str(item.get("display_name") or ""),
            str(item.get("current_display_name") or ""),
            *(str(value) for value in (item.get("aliases") or [])),
        }
        if selector == sender or selector in names:
            exact.append(sender)
    exact = list(dict.fromkeys(value for value in exact if value))
    if not exact:
        raise typer.BadParameter("member selector did not match an exact member id or name")
    if len(exact) != 1:
        raise typer.BadParameter("member selector is ambiguous; use the exact sender wxid")
    return exact[0]


def _member_kb_selected_group_ids(conn) -> list[str]:
    selectors = [str(item) for item in settings.groups if str(item).strip()]
    if not selectors:
        return []
    placeholders = ",".join("?" for _ in selectors)
    rows = conn.execute(
        f"""
        SELECT DISTINCT COALESCE(ga.canonical_group_id, m.group_id) AS group_id
          FROM messages m
          LEFT JOIN group_aliases ga ON ga.alias_id=m.group_id
         WHERE m.group_id IN ({placeholders})
            OR m.group_name IN ({placeholders})
            OR EXISTS (
                SELECT 1 FROM raw_group_authorizations rga
                 WHERE rga.canonical_group_id=COALESCE(ga.canonical_group_id,m.group_id)
                   AND (rga.canonical_group_id IN ({placeholders})
                        OR rga.display_name IN ({placeholders}))
            )
        """,
        selectors * 4,
    ).fetchall()
    group_ids = [str(row["group_id"]) for row in rows]
    if settings.raw_wechat_enabled:
        authorized = {
            str(row["canonical_group_id"])
            for row in conn.execute(
                """
                SELECT canonical_group_id FROM raw_group_authorizations
                 WHERE account_fingerprint=? AND enabled=1
                """,
                (settings.raw_wechat_account,),
            ).fetchall()
        }
        group_ids = [group for group in group_ids if group in authorized]
    return sorted(set(group_ids))


def _require_member_kb_selected_group(conn, group_id: str) -> None:
    if group_id not in _member_kb_selected_group_ids(conn):
        raise typer.BadParameter(
            "--group-id is not in the current exact member-knowledge authorization"
        )


@member_kb_app.command("status")
def member_kb_status(group_id: str = typer.Option("", "--group-id")) -> None:
    """Show derived-profile progress without printing chat content."""
    init_db()
    with get_conn() as conn:
        if group_id:
            _require_member_kb_selected_group(conn, group_id)
        where = "WHERE p.group_id=?" if group_id else ""
        params = (group_id,) if group_id else ()
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS profiles,
                   COALESCE(SUM(CASE WHEN s.cursor_msg_id > 0 THEN 1 ELSE 0 END), 0) AS initialized,
                   COALESCE(MAX(p.updated_at), 0) AS last_updated
              FROM member_profiles p
              LEFT JOIN member_update_state s
                ON s.group_id=p.group_id AND s.sender_wxid=p.sender_wxid
              {where}
            """,
            params,
        ).fetchone()
    typer.echo(json.dumps(dict(row), ensure_ascii=False))


def _run_member_kb_once(*, group_id: str, member: str) -> dict:
    from .member_knowledge import run_due_member_updates, run_member_update

    init_db()
    with get_conn() as conn:
        selected_groups = _member_kb_selected_group_ids(conn)
        if group_id:
            _require_member_kb_selected_group(conn, group_id)
        if not group_id and not selected_groups:
            raise typer.BadParameter(
                "no exact groups are selected; configure a group or pass an authorized --group-id"
            )
        llm = _member_kb_llm()
        if member:
            if not group_id:
                raise typer.BadParameter("--group-id is required with --member")
            sender_wxid = _resolve_member_selector(conn, group_id, member)
            return run_member_update(
                conn,
                group_id,
                sender_wxid,
                llm,
                chunk_chars=settings.member_kb_chunk_chars,
                retries=settings.member_kb_retries,
            )
        return run_due_member_updates(
            conn,
            llm,
            group_ids=[group_id] if group_id else selected_groups,
            chunk_chars=settings.member_kb_chunk_chars,
            retries=settings.member_kb_retries,
        )


@member_kb_app.command("bootstrap")
def member_kb_bootstrap(
    group_id: str = typer.Option("", "--group-id"),
    member: str = typer.Option("", "--member"),
) -> None:
    """Resume full-history profile construction from durable member cursors."""
    typer.echo(json.dumps(_run_member_kb_once(group_id=group_id, member=member), ensure_ascii=False))


@member_kb_app.command("run-once")
def member_kb_run_once(
    group_id: str = typer.Option("", "--group-id"),
    member: str = typer.Option("", "--member"),
) -> None:
    """Process currently pending member messages once."""
    typer.echo(json.dumps(_run_member_kb_once(group_id=group_id, member=member), ensure_ascii=False))


@member_kb_app.command("show")
def member_kb_show(
    group_id: str = typer.Option(..., "--group-id"),
    member: str = typer.Option(..., "--member"),
    messages: bool = typer.Option(False, "--messages", help="Include original messages explicitly"),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
) -> None:
    """Show one profile; raw messages require the explicit --messages flag."""
    from .member_knowledge import get_member_profile, list_member_messages

    init_db()
    with get_conn() as conn:
        _require_member_kb_selected_group(conn, group_id)
        sender_wxid = _resolve_member_selector(conn, group_id, member)
        payload = get_member_profile(conn, group_id, sender_wxid) or {}
        if messages:
            payload = dict(payload)
            payload["messages"] = list_member_messages(
                conn, group_id, sender_wxid, limit=limit
            )
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


@member_kb_app.command("delete")
def member_kb_delete(
    group_id: str = typer.Option(..., "--group-id"),
    member: str = typer.Option(..., "--member"),
    yes: bool = typer.Option(False, "--yes", help="Confirm derived-profile deletion"),
) -> None:
    """Delete derived member knowledge while retaining every raw message."""
    from .member_knowledge import delete_member_profile

    if not yes:
        raise typer.BadParameter("pass --yes to confirm; raw messages will be retained")
    init_db()
    with get_conn() as conn:
        _require_member_kb_selected_group(conn, group_id)
        sender_wxid = _resolve_member_selector(conn, group_id, member)
        delete_member_profile(conn, group_id, sender_wxid, keep_messages=True)
    typer.echo("derived profile deleted; raw messages retained")


@member_kb_app.command("rebuild")
def member_kb_rebuild(
    group_id: str = typer.Option(..., "--group-id"),
    member: str = typer.Option(..., "--member"),
    yes: bool = typer.Option(False, "--yes", help="Confirm full-history rebuild"),
) -> None:
    """Reset one derived profile and rebuild it from its complete message history."""
    from .member_knowledge import reset_member_profile, run_member_update

    if not yes:
        raise typer.BadParameter("pass --yes to confirm the full-history rebuild")
    init_db()
    with get_conn() as conn:
        _require_member_kb_selected_group(conn, group_id)
        sender_wxid = _resolve_member_selector(conn, group_id, member)
        reset_member_profile(conn, group_id, sender_wxid)
        result = run_member_update(
            conn,
            group_id,
            sender_wxid,
            _member_kb_llm(),
            chunk_chars=settings.member_kb_chunk_chars,
            retries=settings.member_kb_retries,
        )
    typer.echo(json.dumps(result, ensure_ascii=False))


def _configure_stdio_utf8() -> None:
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _self_command(*args: str) -> list[str]:
    """Launch this CLI from source or from the frozen Windows executable."""
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return ["uv", "run", "wechat-oracle", *args]


def _self_command_text(*args: str) -> str:
    return subprocess.list2cmdline(_self_command(*args))


def _env_bool(value: bool) -> str:
    return "True" if value else "False"


def _write_env(path: Path, values: dict[str, str], *, force: bool) -> None:
    if path.exists() and not force:
        typer.echo(f"{path} already exists; use --force to overwrite it.")
        raise typer.Exit(1)
    lines = [
        "# Generated by `wechat-oracle setup`.",
        "# Edit values here or override them with WO_* environment variables.",
        "",
    ]
    for key, value in values.items():
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _weflow_setup_hint() -> str:
    return (
        "Install and start WeFlow first: https://github.com/hicccc77/WeFlow\n"
        "In WeFlow, open settings, enable the HTTP API service, then copy the "
        "access token into WO_WEFLOW_TOKEN."
    )


@app.command("setup")
def setup(
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite an existing .env"),
) -> None:
    """Interactive first-run setup. Writes a minimal `.env` in the repo root."""
    env_path = Path(".env")
    if env_path.exists() and not force:
        typer.echo(f"{env_path} already exists; use --force to overwrite it.")
        raise typer.Exit(1)
    typer.echo("WeChat Oracle setup")
    typer.echo("Press Enter to accept defaults. Secrets are written only to .env.")
    typer.echo("")
    typer.echo("Core runtime: local SQLite memory + OpenAI-compatible API.")
    typer.echo("Local WeChat database access is optional and read-only.")
    typer.echo("")

    use_local_db = typer.confirm(
        "Read and continuously monitor the local WeChat chat database?",
        default=settings.raw_wechat_enabled,
    )
    ingest_backend = "wx4py" if use_local_db else typer.prompt(
        "Ingest backend (weflow/wx4py)", default=settings.ingest_backend
    ).strip().lower()
    if ingest_backend not in {"weflow", "wx4py"}:
        typer.echo("Ingest backend must be 'weflow' or 'wx4py'.")
        raise typer.Exit(1)
    weflow_token = (
        typer.prompt("WeFlow token", default=settings.weflow_token or "", hide_input=True)
        if ingest_backend == "weflow" else ""
    )
    groups = "" if use_local_db else typer.prompt(
        "Groups to monitor (comma-separated)",
        default=",".join(settings.groups),
        show_default=False,
    )
    bot_name = typer.prompt("Bot group nickname (WO_BOT_NAME)", default=settings.bot_name or "")
    reply_backend = typer.prompt(
        "Reply backend (uia-direct/wx4py/stdout)",
        default=settings.reply_backend or "wx4py",
    ).strip().lower()
    if reply_backend not in {"uia-direct", "wx4py", "stdout"}:
        typer.echo("Reply backend must be 'uia-direct', 'wx4py', or 'stdout'.")
        raise typer.Exit(1)
    reply = reply_backend != "stdout"

    backend = "native"
    hourly_summary = typer.confirm("Send a summary after each completed hour?", default=False)
    daily_summary = typer.confirm("Send the previous-day summary after midnight?", default=False)
    values: dict[str, str] = {
        "WO_WEFLOW_BASE_URL": settings.weflow_base_url,
        "WO_WEFLOW_TOKEN": weflow_token,
        "WO_INGEST_BACKEND": ingest_backend,
        "WO_GROUPS": groups,
        "WO_BOT_NAME": bot_name,
        "WO_REPLY": _env_bool(reply),
        "WO_REPLY_BACKEND": reply_backend,
        "WO_REPLY_MENTION_POLICY": settings.reply_mention_policy,
        "WO_REPLY_ALLOWED_GROUPS": groups,
        "WO_REPLY_FAIL_CLOSED": _env_bool(settings.reply_fail_closed),
        "WO_AGENT_BACKEND": backend,
        "WO_AGENT_BASE_PROBABILITY": str(settings.agent_base_probability),
        "WO_AGENT_PROACTIVE_MODE": settings.agent_proactive_mode,
        "WO_AGENT_RECENT_CONTEXT_CHAT": str(settings.agent_recent_context_chat),
        "WO_LLM_MAX_TOKENS": str(settings.llm_max_tokens),
        "WO_LLM_WRITE_MAX_TOKENS": str(settings.llm_write_max_tokens or ""),
        "WO_AGENT_LURK_ENABLED": _env_bool(settings.agent_lurk_enabled),
        "WO_AGENT_LURK_INTERVAL_SECONDS": str(settings.agent_lurk_interval_seconds),
        "WO_AGENT_LURK_MIN_NEW_MESSAGES": str(settings.agent_lurk_min_new_messages),
        "WO_RAW_WECHAT_ENABLED": _env_bool(use_local_db),
        "WO_RAW_WECHAT_ACCOUNT": "",
        "WO_RAW_WECHAT_SYNC_INTERVAL_SECONDS": str(settings.raw_wechat_sync_interval_seconds),
        "WO_HOURLY_SUMMARY_ENABLED": _env_bool(hourly_summary),
        "WO_DAILY_SUMMARY_ENABLED": _env_bool(daily_summary),
        "WO_SUMMARY_SYNC_GRACE_SECONDS": str(settings.summary_sync_grace_seconds),
        # Member profiling stays off during first-run setup. Enable it later
        # from the dashboard, where archived-message counts and the privacy
        # warning can be shown before consent is persisted.
        "WO_MEMBER_KB_ENABLED": "False",
        "WO_MEMBER_KB_INTERVAL_SECONDS": str(settings.member_kb_interval_seconds),
        "WO_MEMBER_KB_CHUNK_CHARS": str(settings.member_kb_chunk_chars),
        "WO_MEMBER_KB_MAX_CONCURRENCY": str(settings.member_kb_max_concurrency),
        "WO_MEMBER_KB_RETRIES": str(settings.member_kb_retries),
    }

    values.update(
        {
            "WO_LLM_PROVIDER": "openai-compatible",
            "WO_LLM_API_KEY": typer.prompt(
                "LLM API key",
                default=settings.llm_api_key or "",
                hide_input=True,
            ),
            "WO_LLM_ENDPOINT": typer.prompt(
                "LLM endpoint",
                default=settings.llm_endpoint,
            ),
            "WO_LLM_MODEL": typer.prompt("LLM model", default=settings.llm_model),
            "WO_LLM_JSON_MODE": settings.llm_json_mode,
        }
    )

    if typer.confirm("Configure optional vision model for image reading?", default=False):
        values.update(
            {
                "WO_VISION_PROVIDER": settings.vision_provider,
                "WO_VISION_API_KEY": typer.prompt(
                    "Vision API key",
                    default=settings.vision_api_key or "",
                    hide_input=True,
                ),
                "WO_VISION_ENDPOINT": typer.prompt(
                    "Vision endpoint",
                    default=settings.vision_endpoint,
                ),
                "WO_VISION_MODEL": typer.prompt(
                    "Vision model",
                    default=settings.vision_model,
                ),
                "WO_VISION_MAX_IMAGES": str(settings.vision_max_images),
                "WO_VISION_MAX_TOKENS": str(settings.vision_max_tokens or ""),
            }
        )

    _write_env(env_path, values, force=force)
    os.environ.update(values)
    from .config import reload_settings
    reload_settings()
    init_db()
    if use_local_db:
        _interactive_raw_group_setup(env_path)
    typer.echo(f"\nwrote {env_path}")
    typer.echo("next:")
    typer.echo(f"  {_self_command_text('doctor')}")
    typer.echo(f"  {_self_command_text('run')}")


def _interactive_raw_group_setup(env_path: Path) -> None:
    """Discover one account and let the operator authorize exact chatrooms."""
    from .config_store import _update_env_file
    from .raw_wechat.cli import (
        _account_lock,
        _account_map,
        _authorize_group,
        _cleanup_decrypted,
        _latest_contact,
        _resolve_install_root,
        _select_account,
        _unlock_contact,
    )
    from .raw_wechat.importer import list_groups
    from .raw_wechat.inventory import discover_message_databases
    from .raw_wechat.profile_41155 import verify_install

    typer.echo("\nScanning the reviewed WeChat 4 local database layout...")
    candidates = discover_message_databases()
    accounts = _account_map(candidates)
    if not accounts:
        typer.echo("No supported local WeChat database was found. You can retry later from `raw scan`.")
        return
    if len(accounts) == 1:
        account = next(iter(accounts))
    else:
        typer.echo("Detected anonymous accounts:")
        for index, fingerprint in enumerate(sorted(accounts), 1):
            typer.echo(f"  {index}. {fingerprint} ({len(accounts[fingerprint])} shards)")
        selected = typer.prompt("Account number", type=int)
        ordered = sorted(accounts)
        if selected < 1 or selected > len(ordered):
            raise typer.BadParameter("account number is out of range")
        account = ordered[selected - 1]
    _, account_candidates = _select_account(candidates, account)
    verify_install(_resolve_install_root(settings.raw_wechat_install_root))
    typer.echo("Reading the current contact snapshot; this may take a while on first run...")
    with _account_lock(settings.raw_wechat_workspace, account):
        unlocked = _unlock_contact(account_candidates[0], settings.raw_wechat_workspace)
        if "contact.db" in unlocked["failures"]:
            raise RuntimeError("the current contact database could not be verified")
        message_dbs: list[Path] = []
        contact_db = _latest_contact(settings.raw_wechat_workspace, account)
        options = list_groups(contact_db)
    if not options:
        raise RuntimeError("no joined WeChat groups were found in the current contact snapshot")
    typer.echo("Selectable groups:")
    for index, option in enumerate(options, 1):
        typer.echo(f"  {index}. {option.display_name}  [{option.canonical_group_id}]")
    raw = typer.prompt("Group numbers to monitor/summarize (comma-separated)")
    indexes: list[int] = []
    for part in raw.split(","):
        value = int(part.strip())
        if value < 1 or value > len(options):
            raise typer.BadParameter("group number is out of range")
        if value not in indexes:
            indexes.append(value)
    selected_options = [options[index - 1] for index in indexes]
    for option in selected_options:
        _authorize_group(
            workspace=settings.raw_wechat_workspace,
            account_fingerprint=account,
            archive_path=settings.db_path,
            canonical_group_id=option.canonical_group_id,
            cleanup=False,
        )
    _cleanup_decrypted(message_dbs, contact_db)
    updates = {
        "WO_RAW_WECHAT_ACCOUNT": account,
        "WO_GROUPS": json.dumps(
            [option.display_name for option in selected_options], ensure_ascii=False
        ),
        "WO_REPLY_ALLOWED_GROUPS": json.dumps(
            [option.display_name for option in selected_options], ensure_ascii=False
        ),
    }
    _update_env_file(env_path, updates)
    os.environ.update(updates)
    from .config import reload_settings
    reload_settings()


def _doctor_line(name: str, ok: bool, detail: str) -> bool:
    mark = "OK" if ok else "FAIL"
    typer.echo(f"[{mark}] {name}: {detail}")
    return ok


@app.command("doctor")
def doctor() -> None:
    """Run local readiness checks for setup, WeFlow, DB, LLM backend, and reply path."""
    failures = 0
    typer.echo("WeChat Oracle doctor\n")

    try:
        path = init_db()
        failures += not _doctor_line("database", True, str(path))
    except Exception as e:
        failures += not _doctor_line("database", False, f"{type(e).__name__}: {e}")

    failures += not _doctor_line(
        "WO_BOT_NAME",
        bool(settings.bot_name),
        repr(settings.bot_name) if settings.bot_name else "empty; mentions cannot be recognized",
    )
    failures += not _doctor_line(
        "WO_GROUPS",
        True,
        "all group chats" if not settings.groups else ", ".join(settings.groups),
    )

    if settings.ingest_backend == "wx4py":
        try:
            __import__("wx4py")
            failures += not _doctor_line(
                "UI ingest", bool(settings.groups),
                f"wx4py import ok; groups={settings.groups!r}" if settings.groups else "WO_GROUPS is empty",
            )
        except Exception as e:
            failures += not _doctor_line("UI ingest", False, f"wx4py import failed: {e}")
    elif not settings.weflow_token:
        failures += not _doctor_line(
            "WeFlow API",
            False,
            "WO_WEFLOW_TOKEN is empty. " + _weflow_setup_hint().replace("\n", " "),
        )
    else:
        try:
            from .ingest.live import _build_client
            with _build_client() as client:
                resp = client.get("/api/v1/sessions", params={"limit": 5}, timeout=10.0)
                resp.raise_for_status()
                sessions = resp.json().get("sessions") or []
            failures += not _doctor_line(
                "WeFlow API",
                True,
                f"{settings.weflow_base_url} returned {len(sessions)} session(s)",
            )
        except Exception as e:
            failures += not _doctor_line(
                "WeFlow API",
                False,
                f"{type(e).__name__}: {e}. " + _weflow_setup_hint().replace("\n", " "),
            )

    backend = (settings.agent_backend or "native").lower()
    if backend == "openclaw":
        configured = bool(settings.openclaw_token and settings.openclaw_agent_id)
        failures += not _doctor_line(
            "OpenClaw config",
            configured,
            f"agent={settings.openclaw_agent_id!r} gateway={settings.openclaw_gateway_url}"
            if configured else "WO_OPENCLAW_TOKEN or WO_OPENCLAW_AGENT_ID is empty",
        )
        if configured:
            try:
                url = f"{settings.openclaw_gateway_url.rstrip('/')}/v1/chat/completions"
                payload = {
                    "model": f"openclaw/{settings.openclaw_agent_id}",
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 16,
                }
                resp = httpx.post(
                    url,
                    json=payload,
                    headers={"Authorization": f"Bearer {settings.openclaw_token}"},
                    timeout=min(30.0, settings.openclaw_timeout_seconds),
                )
                failures += not _doctor_line(
                    "OpenClaw gateway",
                    resp.status_code == 200,
                    f"HTTP {resp.status_code}",
                )
            except Exception as e:
                failures += not _doctor_line("OpenClaw gateway", False, f"{type(e).__name__}: {e}")
    elif backend == "pi":
        from .llm import PiRpcLLM
        client = PiRpcLLM(
            executable=settings.pi_executable,
            provider=settings.pi_provider,
            model=settings.pi_model,
            thinking=settings.pi_thinking,
            timeout_seconds=settings.pi_timeout_seconds,
        )
        available, detail = client.check_available()
        failures += not _doctor_line("Pi RPC", available, detail)
    else:
        failures += not _doctor_line(
            "LLM config",
            bool(settings.llm_api_key),
            f"{settings.llm_endpoint} model={settings.llm_model!r}"
            if settings.llm_api_key else "WO_LLM_API_KEY is empty",
        )

    if settings.reply and settings.reply_backend in {"wx4py", "uia-direct"}:
        failures += not _doctor_line(
            "reply allowlist",
            bool(settings.reply_allowed_groups),
            ", ".join(settings.reply_allowed_groups) if settings.reply_allowed_groups else "WO_REPLY_ALLOWED_GROUPS is empty",
        )
        try:
            __import__("wx4py")
            failures += not _doctor_line(
                "reply backend",
                True,
                f"{settings.reply_backend}: wx4py import ok",
            )
        except Exception as e:
            failures += not _doctor_line("reply backend", False, f"wx4py import failed: {e}")
    else:
        failures += not _doctor_line(
            "reply backend",
            True,
            "stdout/local only" if not settings.reply or settings.reply_backend == "stdout" else settings.reply_backend,
        )

    try:
        with get_conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            groups = conn.execute(
                "SELECT COUNT(DISTINCT group_id) FROM messages WHERE group_id IS NOT NULL"
            ).fetchone()[0]
        _doctor_line("message archive", True, f"{total} message(s), {groups} group(s)")
    except Exception as e:
        failures += not _doctor_line("message archive", False, f"{type(e).__name__}: {e}")

    typer.echo("")
    if failures:
        typer.echo(f"doctor finished with {failures} failing check(s)")
        raise typer.Exit(1)
    typer.echo("doctor passed")


def _child_env_for_dashboard() -> dict[str, str]:
    env = dict(os.environ)
    # The parent dashboard decodes child stdout as UTF-8. On Windows, Python
    # child processes otherwise default to the active ANSI code page (often
    # cp936/GBK), which corrupts Chinese when read through the pipe.
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _start_log_reader(
    name: str,
    proc: subprocess.Popen[str],
    log_lines: deque[str],
    lock: threading.Lock,
) -> threading.Thread:
    def read() -> None:
        assert proc.stdout is not None
        formatter = _DashboardLogFormatter(name)
        for line in proc.stdout:
            text = line.rstrip()
            if not text:
                continue
            with lock:
                for rendered in formatter.feed(text):
                    log_lines.append(rendered)
            if _looks_like_model_call_log(text):
                try:
                    from .run_tui import request_balance_refresh

                    request_balance_refresh()
                except Exception:
                    pass
        with lock:
            for rendered in formatter.flush():
                log_lines.append(rendered)

    thread = threading.Thread(target=read, name=f"wechat-oracle-{name}-log", daemon=True)
    thread.start()
    return thread


def _looks_like_model_call_log(text: str) -> bool:
    return (
        " agent:" in text
        or "agent ::" in text
        or "agent :: " in text
        or "openclaw agent ::" in text
        or "openclaw agent" in text
        or "llm_filter" in text
    )


def _dashboard_log_line(source: str, text: str) -> str:
    stamp = datetime.now().strftime("%H:%M:%S")
    compact = _compact_child_log(source, text)
    if compact is None:
        return ""
    return f"{stamp} {_dashboard_source_label(source)} │ {compact}"


class _DashboardLogFormatter:
    """Stateful formatter for child stdout shown in the TUI.

    Dispatcher agent output is printed as a small burst of related lines. Group
    those bursts into a single card-like block, while leaving ordinary process
    logs as compact one-line records.
    """

    def __init__(self, source: str) -> None:
        self._source = source
        self._pending_stamp: str | None = None
        self._pending_lines: list[str] = []

    def feed(self, text: str) -> list[str]:
        if not text.strip():
            return []
        if self._source != "dispatcher":
            line = _dashboard_log_line(self._source, text)
            return [line] if line else []
        if text.startswith("msg=") or text.startswith("followup="):
            out = self.flush()
            self._pending_stamp = datetime.now().strftime("%H:%M:%S")
            self._pending_lines = [text]
            return out
        if self._pending_lines and (text.startswith("  ") or text.startswith("    ")):
            self._pending_lines.append(text)
            stripped = text.strip()
            if stripped.startswith("send:") or stripped.startswith("followup:"):
                return self.flush()
            return []
        out = self.flush()
        line = _dashboard_log_line(self._source, text)
        if line:
            out.append(line)
        return out

    def flush(self) -> list[str]:
        if not self._pending_lines:
            return []
        rendered = _dashboard_event_card(
            self._source,
            self._pending_stamp or datetime.now().strftime("%H:%M:%S"),
            self._pending_lines,
        )
        self._pending_stamp = None
        self._pending_lines = []
        return [rendered]


_EVENT_START_RE = re.compile(
    r"^msg=(?P<msg_id>\d+)\s+group=(?P<group>.*?)\s+from=(?P<sender>.*?)\s+type=(?P<msg_type>\S+)"
)
_FOLLOWUP_START_RE = re.compile(
    r"^followup=(?P<job_id>\d+)\s+(?:group=(?P<group>.*?)\s+)?(?:kind=(?P<kind>\w+)\s+seq=(?P<seq>\S+)|skipped status=(?P<status>\S+))"
)


def _dashboard_event_card(source: str, stamp: str, lines: list[str]) -> str:
    label = _dashboard_source_label(source)
    prefix = f"{stamp} {label} │ "
    cont = " " * len(prefix)
    start = lines[0]
    match = _EVENT_START_RE.match(start)
    if match:
        header = (
            f"msg {match.group('msg_id')}  {match.group('group')}  "
            f"{match.group('sender')}  [{match.group('msg_type')}]"
        )
    elif follow := _FOLLOWUP_START_RE.match(start):
        if follow.group("status"):
            header = f"follow-up {follow.group('job_id')} skipped: {follow.group('status')}"
        else:
            header = (
                f"follow-up {follow.group('job_id')}  {follow.group('group') or '?'}  "
                f"{follow.group('kind')}  {follow.group('seq')}"
            )
    else:
        header = start
    body: list[str] = [prefix + f"┌─ {header}"]
    closing = cont + "└─"
    for raw in lines[1:]:
        line = raw.strip()
        if line.startswith("trigger:"):
            body.append(cont + "│ trigger " + line.removeprefix("trigger:").strip())
        elif line.startswith("text:"):
            body.extend(_dashboard_wrap_field(cont, "text", line.removeprefix("text:").strip(), 96))
        elif line.startswith("agent:"):
            body.append(cont + "│ agent   " + line.removeprefix("agent:").strip())
        elif line.startswith("openclaw:"):
            body.append(cont + "│ llm     " + line.removeprefix("openclaw:").strip())
        elif line.startswith("tools:"):
            body.extend(_dashboard_wrap_field(cont, "tools", line.removeprefix("tools:").strip(), 120))
        elif line.startswith("memory:"):
            body.extend(_dashboard_wrap_field(cont, "memory", line.removeprefix("memory:").strip(), 120))
        elif line.startswith("intent:"):
            body.extend(_dashboard_wrap_field(cont, "intent", line.removeprefix("intent:").strip(), 96))
        elif line.startswith("reply:"):
            body.extend(_dashboard_wrap_field(cont, "reply", line.removeprefix("reply:").strip(), 120))
        elif line.startswith("send:"):
            closing = cont + "└─ send    " + line.removeprefix("send:").strip()
        elif line.startswith("followup:"):
            closing = cont + "└─ " + line
        else:
            body.extend(_dashboard_wrap_field(cont, "", line, 120))
    body.append(closing)
    return "\n".join(body)


def _dashboard_wrap_field(cont: str, label: str, text: str, limit: int) -> list[str]:
    text = " ".join(text.split())
    if len(text) <= limit:
        return [cont + f"│ {label:<7s} {text}"]
    out = [cont + f"│ {label:<7s} {text[:limit]}…"]
    remaining = text[limit:]
    while remaining:
        chunk = remaining[:limit]
        remaining = remaining[limit:]
        out.append(cont + f"│ {'':<7s} {chunk}{'…' if remaining else ''}")
    return out


def _clip_dashboard_payload(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


_LOGURU_LINE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\s*\|\s*"
    r"(?P<level>[A-Z]+)\s*\|\s*(?P<body>.*)$"
)


def _compact_child_log(source: str, text: str) -> str | None:
    if text.startswith("2026-") and " | DEBUG" in text:
        return None
    match = _LOGURU_LINE_RE.match(text)
    if not match:
        return _compact_plain_child_log(source, text)
    level = match.group("level")
    body = _shorten_loguru_body(source, match.group("body"))
    if body is None:
        return None
    return f"{level:<7s} │ {body}"


_LOGURU_BODY_RE = re.compile(
    r"^(?:wechat_oracle\.)?(?P<module>[\w.]+):(?P<func>\w+):(?P<line>\d+)\s+-\s+(?P<msg>.*)$"
)


def _shorten_loguru_body(source: str, body: str) -> str | None:
    match = _LOGURU_BODY_RE.match(body)
    if not match:
        return body
    module = match.group("module").replace("wechat_oracle.", "")
    module = module.removeprefix("ingest.")
    msg = match.group("msg")
    short = _compact_known_message(source, module, match.group("func"), msg)
    if short is None:
        return None
    return short or f"{module}.{match.group('func')}  {msg}"


def _compact_plain_child_log(source: str, text: str) -> str | None:
    if " - " in text and ("wechat_oracle." in text or "wx4py." in text):
        _, _, body = text.partition(" - ")
        return _compact_known_message(source, "", "", body) or body
    return _clip_dashboard_payload(text, 180)


def _compact_known_message(source: str, module: str, func: str, msg: str) -> str | None:
    if source == "live":
        m = re.search(r"wrote (?P<attempt>\d+) messages \((?P<new>\d+) new, (?P<dup>\d+) duplicates skipped\)(?:; \+(?P<fwd>\d+) forwarded items)?", msg)
        if m:
            fwd = f", +{m.group('fwd')} forward" if m.group("fwd") else ""
            return f"DB write  +{m.group('new')} new, {m.group('dup')} dup{fwd}"
        m = re.search(r"(?P<group>.+?): \+(?P<n>\d+) new", msg)
        if m:
            return f"new message  {m.group('group')}  +{m.group('n')}"
        m = re.search(r"watching: (?P<group>.+?) \((?P<id>[^)]+)\) - (?P<n>\d+) members loaded", msg)
        if m:
            return f"watching  {m.group('group')}  {m.group('n')} members"
        if "SSE: connecting" in msg:
            return "SSE connecting"
        if "WO_GROUPS is empty" in msg:
            return "watching all WeFlow group sessions"
        if "mm worker thread started" in msg:
            return "media worker started"
        if msg.startswith("mm worker:"):
            return msg.replace("mm worker:", "media worker")
        if "loading RapidOCR" in msg:
            return "OCR model loading"
        if "run_ocr" in msg or "run_asr" in msg:
            return _clip_dashboard_payload(msg, 160)
    if source == "dispatcher":
        if msg.startswith("dispatcher:"):
            return _clip_dashboard_payload("dispatcher ready  " + _summarize_dispatcher_ready(msg), 180)
        if "bot_wxid resolved:" in msg:
            return msg.replace("bot_wxid resolved:", "bot wxid").replace(" (auto-discovered from messages)", "")
        if msg.startswith("continuation scheduler submitted"):
            return msg.replace("continuation scheduler", "continuation")
        if msg.startswith("lurk scheduler submitted"):
            return msg.replace("lurk scheduler", "lurk")
    if source == "mm":
        if msg.startswith("mm worker:"):
            return msg.replace("mm worker:", "media worker")
        if "loading RapidOCR" in msg:
            return "OCR model loading"
        if "run_ocr" in msg or "run_asr" in msg:
            return _clip_dashboard_payload(msg, 160)
    return ""


def _summarize_dispatcher_ready(msg: str) -> str:
    keep: list[str] = []
    for key in ("bot=", "model=", "agent_backend=", "workers=", "interval=", "replier="):
        m = re.search(rf"{key}([^ ]+)", msg)
        if m:
            keep.append(f"{key}{m.group(1)}")
    return " ".join(keep) or msg


def _dashboard_source_label(source: str) -> str:
    label = {
        "run": "SYSTEM",
        "live": "INGEST",
        "dispatcher": "DISPATCH",
        "mm": "MEDIA",
    }.get(source, source[:8].upper())
    return f"{label:<8s}"


def _dashboard_process_label(name: str) -> str:
    return {
        "live": "INGEST",
        "dispatcher": "DISPATCH",
        "mm": "MEDIA",
        "raw-sync": "LOCAL DB",
    }.get(name, name)


def _terminate_process_tree(proc: subprocess.Popen[str], *, force: bool = False) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        cmd = ["taskkill", "/PID", str(proc.pid), "/T"]
        # uv/console-script wrappers keep the real Python worker as a child or
        # grandchild on Windows. Non-forced taskkill often leaves that tree
        # alive, which makes `run` sit on the shutdown deadline every time.
        cmd.append("/F")
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    if force:
        proc.kill()
    else:
        proc.send_signal(signal.SIGTERM)


@app.command("run")
def run(
    skip_init: bool = typer.Option(False, "--skip-init", help="Do not run init-db before starting"),
    plain: bool = typer.Option(False, "--plain", help="Do not start the TUI; let child processes write directly"),
) -> None:
    """Start live ingest and dispatcher together. Ctrl+C stops both."""
    _configure_stdio_utf8()
    if not skip_init:
        init_db()
    settings.ensure_dirs()
    procs: dict[str, subprocess.Popen[str]] = {}
    ingest_command = "ui-live" if settings.ingest_backend == "wx4py" else "live"
    commands = {
        "live": _self_command("ingest", ingest_command),
        "dispatcher": _self_command("dispatcher"),
    }
    if settings.raw_wechat_enabled:
        commands["raw-sync"] = _self_command("raw", "run")
    optional_processes = {"raw-sync"}
    restart_after: dict[str, float] = {}
    process_lock = threading.Lock()
    manual_restarts: set[str] = set()
    log_lines: deque[str] = deque(maxlen=1000)
    log_lock = threading.Lock()
    readers: list[threading.Thread] = []
    if plain:
        typer.echo("starting WeChat Oracle:")

    def start_process(name: str) -> subprocess.Popen[str]:
        cmd = commands[name]
        env = _child_env_for_dashboard()
        if plain:
            typer.echo(f"  {name}: {' '.join(cmd)}")
            proc = subprocess.Popen(cmd, env=env)
        else:
            with log_lock:
                log_lines.append(
                    _dashboard_log_line("run", f"starting {_dashboard_process_label(name)}  {' '.join(cmd)}")
                )
            proc = subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            readers.append(_start_log_reader(name, proc, log_lines, log_lock))
        with process_lock:
            procs[name] = proc
        return proc

    def restart_process(name: str) -> None:
        with process_lock:
            proc = procs.get(name)
            manual_restarts.add(name)
        if proc is not None:
            with log_lock:
                log_lines.append(_dashboard_log_line("run", f"restarting {_dashboard_process_label(name)}"))
            _terminate_process_tree(proc)
            deadline = time.time() + (2.0 if os.name == "nt" else 10.0)
            while proc.poll() is None and time.time() < deadline:
                time.sleep(0.1)
            if proc.poll() is None:
                _terminate_process_tree(proc, force=True)
        start_process(name)
        with process_lock:
            manual_restarts.discard(name)
        with log_lock:
            log_lines.append(_dashboard_log_line("run", f"{_dashboard_process_label(name)} restarted"))

    for name in commands:
        start_process(name)

    def process_rows() -> list[tuple[str, int | None, int | None]]:
        with process_lock:
            return [(name, proc.pid, proc.poll()) for name, proc in procs.items()]

    stop_requested = False
    try:
        if plain:
            while True:
                with process_lock:
                    current_procs = list(procs.items())
                for name, proc in current_procs:
                    code = proc.poll()
                    if code is not None:
                        if name in optional_processes:
                            due = restart_after.setdefault(name, time.time() + 30)
                            if time.time() >= due:
                                typer.echo(f"{_dashboard_process_label(name)} restarting after exit {code}")
                                start_process(name)
                                restart_after.pop(name, None)
                            continue
                        typer.echo(
                            f"{_dashboard_process_label(name)} exited with code {code}; stopping remaining processes"
                        )
                        raise KeyboardInterrupt
                time.sleep(1.0)
        else:
            from .run_tui import RunDashboard, status_lines_for_processes
            from .config_store import (
                AgentRuntimeConfig,
                load_agent_runtime_config,
                save_agent_runtime_config,
            )

            def apply_agent_config(config: AgentRuntimeConfig) -> None:
                from .config import reload_settings

                updates = save_agent_runtime_config(config)
                os.environ.update(updates)
                reload_settings()
                with log_lock:
                    log_lines.append(
                        _dashboard_log_line(
                            "run",
                            "config saved to .env; restarting runtime processes",
                        )
                    )
                if settings.raw_wechat_enabled:
                    commands["raw-sync"] = _self_command("raw", "run")
                    with process_lock:
                        raw_proc = procs.get("raw-sync")
                    if raw_proc is None or raw_proc.poll() is not None:
                        start_process("raw-sync")
                else:
                    commands.pop("raw-sync", None)
                    with process_lock:
                        raw_proc = procs.pop("raw-sync", None)
                    if raw_proc is not None:
                        _terminate_process_tree(raw_proc)
                restart_process("live")
                restart_process("dispatcher")

            app = RunDashboard(
                status_provider=lambda: status_lines_for_processes(
                    process_rows(),
                    load_agent_runtime_config(),
                ),
                log_buffer=log_lines,
                agent_config_provider=load_agent_runtime_config,
                agent_config_save=apply_agent_config,
            )
            def watch_children() -> None:
                while True:
                    with process_lock:
                        current_procs = list(procs.items())
                        restarting = set(manual_restarts)
                    for child_name, child_proc in current_procs:
                        code = child_proc.poll()
                        if code is not None:
                            with process_lock:
                                if child_name in restarting or procs.get(child_name) is not child_proc:
                                    continue
                            if child_name in optional_processes:
                                due = restart_after.setdefault(child_name, time.time() + 30)
                                if time.time() >= due:
                                    with log_lock:
                                        log_lines.append(
                                            _dashboard_log_line(
                                                "run",
                                                f"restarting {_dashboard_process_label(child_name)} after exit {code}",
                                            )
                                        )
                                    start_process(child_name)
                                    restart_after.pop(child_name, None)
                                continue
                            with log_lock:
                                log_lines.append(
                                    _dashboard_log_line(
                                        "run",
                                        f"{_dashboard_process_label(child_name)} exited with code {code}; stopping remaining processes",
                                    )
                                )
                            try:
                                app.call_from_thread(app.exit)
                            except Exception:
                                pass
                            return
                    time.sleep(1.0)

            threading.Thread(
                target=watch_children,
                name="wechat-oracle-run-watch",
                daemon=True,
            ).start()
            app.run()
            stop_requested = True
    except KeyboardInterrupt:
        stop_requested = True
    finally:
        if not stop_requested and not plain:
            with log_lock:
                log_lines.append(_dashboard_log_line("run", "stopping WeChat Oracle"))
        if plain:
            typer.echo("stopping WeChat Oracle...")
        with process_lock:
            current_procs = list(procs.values())
        for proc in current_procs:
            _terminate_process_tree(proc)
        deadline = time.time() + (2.0 if os.name == "nt" else 10.0)
        for proc in current_procs:
            while proc.poll() is None and time.time() < deadline:
                time.sleep(0.1)
            if proc.poll() is None:
                _terminate_process_tree(proc, force=True)
        if plain:
            typer.echo("stopped")
        else:
            with log_lock:
                log_lines.append(_dashboard_log_line("run", "stopped"))


@verify_app.command("roundtrip")
def verify_roundtrip() -> None:
    """Check whether WeFlow SSE echoes the bot's own replies back into the
    messages table. Required for reply-to-bot triggers and bot_wxid
    auto-discovery.

    Run this AFTER you've @ed the bot a few times in a watched group and
    the bot has replied. Reads `messages` looking for rows where
    `sender_display == WO_BOT_NAME` (i.e. the bot's own messages).
    """
    if not settings.bot_name:
        typer.echo("WARNING:  WO_BOT_NAME is empty - set it to the bot's group nickname first.")
        raise typer.Exit(1)
    init_db()
    with get_conn() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE sender_display = ?",
            (settings.bot_name,),
        ).fetchone()["n"]
        wxid_row = conn.execute(
            "SELECT sender_wxid FROM messages "
            "WHERE sender_display = ? AND sender_wxid IS NOT NULL AND sender_wxid != '' "
            "ORDER BY t DESC LIMIT 1",
            (settings.bot_name,),
        ).fetchone()
        recent = conn.execute(
            "SELECT msg_id, group_name, t, content_text, sender_wxid FROM messages "
            "WHERE sender_display = ? ORDER BY t DESC LIMIT 5",
            (settings.bot_name,),
        ).fetchall()

    typer.echo(f"bot_name = {settings.bot_name!r}")
    typer.echo(f"messages where sender_display matches: {total}")
    if total == 0:
        typer.echo("")
        typer.echo("ERROR: No bot messages echoed back.")
        typer.echo("   Either the bot hasn't replied yet, OR WeFlow SSE doesn't")
        typer.echo("   roundtrip self-sent messages. Reply-to-bot trigger will be")
        typer.echo("   permanently disabled in this case - set WO_BOT_WXID manually.")
        return
    if wxid_row is None:
        typer.echo("")
        typer.echo("WARNING:  Bot messages found but their sender_wxid is NULL.")
        typer.echo("   Auto-discovery can't recover wxid; set WO_BOT_WXID manually.")
    else:
        typer.echo(f"discovered bot wxid: {wxid_row['sender_wxid']}")
        if not settings.bot_wxid:
            typer.echo(f"  consider setting WO_BOT_WXID={wxid_row['sender_wxid']} in .env")
            typer.echo("    (skips the wxid discovery delay on cold start)")
        elif settings.bot_wxid != wxid_row["sender_wxid"]:
            typer.echo(f"WARNING:  WO_BOT_WXID={settings.bot_wxid!r} but DB shows {wxid_row['sender_wxid']!r}")
    typer.echo("")
    typer.echo("recent bot messages:")
    from datetime import datetime
    for r in recent:
        ts = datetime.fromtimestamp(int(r["t"])).strftime("%Y-%m-%d %H:%M")
        body = (r["content_text"] or "").replace("\n", " ")[:60]
        typer.echo(f"  [{r['msg_id']}] {ts} ({r['group_name'] or '?'}): {body}")


@worker_app.command("mm")
def worker_mm() -> None:
    """Standalone OCR/ASR worker. Usually you don't need to run this - `ingest
    live` already starts an mm worker thread alongside SSE capture, so a
    normal "live + dispatcher" deployment covers it.

    Use this command when you want to run mm on its own - e.g. to drain a
    backfill queue without ingesting new messages, or to debug OCR/ASR in
    isolation. Long-running. Polls newest-first. Models lazy-load on first
    use: rapidocr-onnxruntime for images, faster-whisper (`small` by default;
    set WO_WHISPER_MODEL=tiny|base|medium|large-v3 to override) for voice.
    Both run locally on CPU - no data leaves the machine.
    """
    from .log_utils import setup_process_log
    from .worker.mm import run_mm_worker
    setup_process_log("mm")
    run_mm_worker()


@app.command("init-db")
def init_db_cmd() -> None:
    """Create / migrate the SQLite database."""
    path = init_db()
    logger.info("db ready at {}", path)


@ingest_app.command("backfill")
def ingest_backfill(
    path: Path = typer.Argument(..., exists=True, readable=True, dir_okay=True),
    fmt: str = typer.Option("jsonl", "--format", "-f", help="weflow | jsonl"),
) -> None:
    """Import historical export file(s) into the messages table."""
    init_db()
    settings.ensure_dirs()
    msgs = import_file(path, fmt, settings.data_dir)
    with get_conn() as conn:
        attempted, inserted = write_messages(conn, msgs)
    typer.echo(f"backfill: attempted={attempted} new={inserted}")


@ingest_app.command("live")
def ingest_live() -> None:
    """Subscribe to WeFlow SSE for new messages in WO_GROUPS, AND run the mm
    OCR/ASR worker in a background thread. Requires WeFlow running.

    Combined process so a normal deployment is just two terminals:
    `ingest live` (this) + `dispatcher`. The mm thread shares the SQLite
    file via WAL - no separate process needed.
    """
    from .ingest.live import run_live
    from .log_utils import setup_process_log
    setup_process_log("live")
    run_live()


@ingest_app.command("ui-live")
def ingest_ui_live() -> None:
    """Read text/link events from exact visible WeChat groups via wx4py UIA."""
    from .ingest.ui_live import run_ui_live
    from .log_utils import setup_process_log
    setup_process_log("live")
    run_ui_live()


@ingest_app.command("ui-probe")
def ingest_ui_probe(
    group: str = typer.Argument(..., help="Exact WeChat group display name"),
) -> None:
    """Open one group read-only and report wx4py compatibility; sends nothing."""
    import json
    from .ingest.ui_live import probe_ui_group
    result = probe_ui_group(group)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("dispatcher")
def dispatcher_cmd() -> None:
    """Watch DB for `@<bot> /find ...` commands; print results to stdout + log.

    Requires WO_BOT_NAME and a configured selected agent backend. Runs in foreground;
    Ctrl+C to stop. Safe to run alongside `ingest live`.
    """
    from .dispatcher import run_dispatcher
    from .log_utils import setup_process_log
    setup_process_log("dispatcher")
    run_dispatcher()


# --- agent memory inspection ----------------------------------------------


@agent_app.command("show")
def agent_show(
    group_id: str = typer.Argument(..., help="messages.group_id of the target group"),
) -> None:
    """Dump persona_drift + group_memory + lurk cursor for one group."""
    from datetime import datetime
    init_db()
    with get_conn() as conn:
        drift = conn.execute(
            "SELECT drift_text, updated_at, last_run_id FROM persona_drift WHERE group_id=?",
            (group_id,),
        ).fetchone()
        memory = conn.execute(
            "SELECT notes_text, size_chars, updated_at, last_run_id FROM group_memory WHERE group_id=?",
            (group_id,),
        ).fetchone()
        lurk_state = conn.execute(
            "SELECT last_msg_id, last_run_id, updated_at FROM agent_lurk_state WHERE group_id=?",
            (group_id,),
        ).fetchone()

    typer.echo(f"=== group_id={group_id!r} ===\n")
    typer.echo("--- persona_drift ---")
    if drift is None or not (drift["drift_text"] or "").strip():
        typer.echo("(empty)\n")
    else:
        ts = datetime.fromtimestamp(drift["updated_at"]).strftime("%Y-%m-%d %H:%M") if drift["updated_at"] else "?"
        typer.echo(f"updated_at={ts}  last_run_id={drift['last_run_id'] or '?'}")
        typer.echo(drift["drift_text"])
        typer.echo("")

    cap = settings.agent_memory_max_chars
    typer.echo(f"--- group_memory (cap {cap} chars) ---")
    if memory is None or not (memory["notes_text"] or "").strip():
        typer.echo("(empty)\n")
    else:
        ts = datetime.fromtimestamp(memory["updated_at"]).strftime("%Y-%m-%d %H:%M") if memory["updated_at"] else "?"
        size = memory["size_chars"] or 0
        pct = size * 100 // cap if cap else 0
        typer.echo(
            f"updated_at={ts}  last_run_id={memory['last_run_id'] or '?'}  "
            f"size={size} chars ({pct}% of cap)"
        )
        typer.echo(memory["notes_text"])

    typer.echo("\n--- lurk_state ---")
    if lurk_state is None:
        typer.echo("(no cursor yet)")
    else:
        ts = datetime.fromtimestamp(lurk_state["updated_at"]).strftime("%Y-%m-%d %H:%M") if lurk_state["updated_at"] else "?"
        typer.echo(
            f"last_msg_id={lurk_state['last_msg_id'] or '?'}  "
            f"last_run_id={lurk_state['last_run_id'] or '?'}  updated_at={ts}"
        )


@agent_app.command("wipe")
def agent_wipe(
    group_id: str = typer.Argument(..., help="messages.group_id of the target group"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
    persona_only: bool = typer.Option(False, "--persona-only", help="Wipe persona_drift only, keep group_memory"),
    memory_only: bool = typer.Option(False, "--memory-only", help="Wipe group_memory only, keep persona_drift"),
) -> None:
    """Clear persona_drift and/or group_memory for one group. Destructive -
    bot resets to its defaults in this group. agent_run_log is NOT touched
    (audit trail stays).
    """
    if persona_only and memory_only:
        typer.echo("WARNING:  --persona-only and --memory-only are mutually exclusive")
        raise typer.Exit(1)
    targets = []
    if not memory_only:
        targets.append("persona_drift")
    if not persona_only:
        targets.append("group_memory")
    if not yes:
        typer.echo(f"about to clear {', '.join(targets)} for group_id={group_id!r}")
        confirm = typer.confirm("proceed?")
        if not confirm:
            typer.echo("aborted")
            raise typer.Exit(0)
    init_db()
    with get_conn() as conn:
        with transaction(conn):
            if "persona_drift" in targets:
                conn.execute("DELETE FROM persona_drift WHERE group_id=?", (group_id,))
            if "group_memory" in targets:
                conn.execute("DELETE FROM group_memory WHERE group_id=?", (group_id,))
    typer.echo(f"wiped: {', '.join(targets)}")


@agent_app.command("lurk")
def agent_lurk(
    group_id: str = typer.Argument(..., help="messages.group_id of the target group"),
) -> None:
    """One-shot 'lurk' run: bot reads new messages since its cursor, may
    search older history for context, and decides whether to update
    group_memory / persona_drift. No reply is ever sent to the group.

    Useful for periodic background memory consolidation (cron this every
    30 min for an active group), or to manually nudge the bot to update
    its impression of group activity after you've imported a backfill.
    """
    from .agent.orchestrator import chat_via_lurk
    from .dispatcher import _build_llm_client, _resolve_bot_wxid
    if not settings.bot_name:
        typer.echo("WARNING:  WO_BOT_NAME is empty - set it before lurking.")
        raise typer.Exit(1)
    init_db()
    settings.ensure_dirs()
    log_path = settings.data_dir / "dispatcher.log"
    llm_log_path = settings.data_dir / "llm_debug.log"
    llm = _build_llm_client()
    with get_conn() as conn:
        bot_wxid = _resolve_bot_wxid(conn, settings.bot_name)
        # Find the group_name for the prompt; OK if missing.
        row = conn.execute(
            "SELECT group_name FROM messages WHERE group_id=? AND group_name IS NOT NULL "
            "ORDER BY t DESC LIMIT 1",
            (group_id,),
        ).fetchone()
        group_name = row["group_name"] if row else None
        trace_block = chat_via_lurk(
            conn=conn,
            llm=llm,
            model=settings.llm_model,
            bot_name=settings.bot_name,
            bot_wxid=bot_wxid,
            group_id=group_id,
            group_name=group_name,
            log_path=log_path,
            llm_log_path=llm_log_path,
        )
    typer.echo(trace_block)


@agent_app.command("ask")
def agent_ask(
    group: str = typer.Argument(..., help="Target group id, group name, or unique fragment"),
    question: list[str] = typer.Argument(..., help="Question/task text"),
    write: bool = typer.Option(
        False,
        "--write",
        help="Allow the local turn to update group_memory / persona_drift",
    ),
    trace: bool = typer.Option(False, "--trace", help="Print the agent trace after the reply"),
) -> None:
    """Ask the agent about one group locally. No WeChat reply is sent."""
    from .agent.local_ask import run_local_ask

    text = " ".join(question).strip()
    if not text:
        typer.echo("question is empty")
        raise typer.Exit(1)
    result = run_local_ask(
        group_selector=group,
        question=text,
        allow_writes=write,
        log_path=settings.data_dir / "dispatcher.log",
        llm_log_path=settings.data_dir / "llm_debug.log",
    )
    typer.echo(f"group: {result.group.label}")
    typer.echo(f"mode: {'local_task/write' if result.allow_writes else 'local_ask/read-only'}")
    typer.echo(f"duration: {result.duration_s:.1f}s")
    typer.echo("")
    typer.echo(result.reply_text or "(no reply)")
    if trace and result.trace_block:
        typer.echo("\n--- trace ---")
        typer.echo(result.trace_block)


def _classify_silent(phase_a_trace: list[dict[str, object]] | None) -> tuple[str, str]:
    """Categorize why an agent run ended without a reply. Returns (label, detail).
    Labels: 'stay_silent' (A) / 'empty' (B) / 'max_steps' (C) / 'unknown'."""
    if not phase_a_trace:
        return "unknown", ""
    # A: explicit termination via stay_silent
    for s in phase_a_trace:
        if s.get("kind") == "terminate" and s.get("reason") == "stay_silent":
            # Reason given by the model lives in the prior tool_call args
            for prior in phase_a_trace:
                if prior.get("kind") == "tool_call" and prior.get("tool") == "stay_silent":
                    args = prior.get("args") or {}
                    return "stay_silent", str(args.get("reason", ""))[:80]
            return "stay_silent", ""
    # C: hit the step cap
    for s in phase_a_trace:
        if s.get("kind") == "max_steps_hit":
            return "max_steps", ""
    # B: a final step with empty content
    for s in phase_a_trace:
        if s.get("kind") == "final" and not s.get("content"):
            retried = any(t.get("kind") == "empty_final_retry" for t in phase_a_trace)
            return "empty", "(retried once)" if retried else ""
    return "unknown", ""


@agent_app.command("show-runs")
def agent_show_runs(
    group_id: str = typer.Argument(..., help="messages.group_id of the target group"),
    limit: int = typer.Option(10, "--limit", "-n"),
) -> None:
    """Recent agent_run_log entries with phase-B writes highlighted.

    Silent runs are tagged with one of three causes:
      stay_silent: model called the stay_silent tool (healthy decision)
      empty:       model returned empty final text without calling stay_silent
                   (likely confused / refused - investigate the trigger msg)
      max_steps:   model burned all tool-calling rounds without emitting text
                   (rare since runtime forces final on last step)
    """
    import json as _json
    from datetime import datetime
    from .agent.memory import list_recent_runs
    init_db()
    with get_conn() as conn:
        rows = list_recent_runs(conn, group_id, limit=limit)
    if not rows:
        typer.echo(f"no agent runs for group_id={group_id!r}")
        return
    for r in rows:
        ts = datetime.fromtimestamp(r["started_at"]).strftime("%Y-%m-%d %H:%M:%S") if r["started_at"] else "?"
        dur = (r["finished_at"] - r["started_at"]) if (r["started_at"] and r["finished_at"]) else 0
        reply_text = r["reply_text"]
        if r["trigger_kind"] == "lurk":
            reply = "(lurk: no chat reply)"
        elif reply_text is None or not reply_text.strip():
            try:
                pa = _json.loads(r["phase_a_trace"] or "[]")
            except _json.JSONDecodeError:
                pa = []
            label, detail = _classify_silent(pa)
            reply = f"(silent: {label}{' - ' + detail if detail else ''})"
        else:
            reply = reply_text.replace("\n", " ")[:80]
        typer.echo(
            f"[{r['run_id']}] {ts}  trigger={r['trigger_kind']:12s}  "
            f"msg_id={r['trigger_msg_id'] or '?':>7}  {dur:.1f}s"
        )
        typer.echo(f"     reply: {reply}")
        # Surface any Phase B writes inline
        try:
            pb = _json.loads(r["phase_b_trace"] or "[]")
        except _json.JSONDecodeError:
            pb = []
        writes = [s for s in pb if s.get("kind") == "tool_call" and s.get("tool", "").startswith("update_")]
        for w in writes:
            args = w.get("args", {})
            preview_key = "drift_text" if w["tool"] == "update_persona_drift" else "notes_text"
            preview = (args.get(preview_key, "") or "").replace("\n", " ")[:80]
            typer.echo(f"     phase B {w['tool']}: {preview}")


@weflow_app.command("find")
def weflow_find(keyword: str = typer.Argument(..., help="Group name / remark / wxid fragment")) -> None:
    """Search both /api/v1/contacts and /api/v1/sessions for a group. Use this to find
    the correct wxid to put in WO_GROUPS when a name doesn't resolve."""
    from .ingest.live import _build_client
    with _build_client() as client:
        cresp = client.get("/api/v1/contacts", params={"keyword": keyword, "limit": 50})
        cresp.raise_for_status()
        contacts = cresp.json().get("contacts") or []
        groups = [c for c in contacts if "@chatroom" in (c.get("username") or "")]
        people = [c for c in contacts if "@chatroom" not in (c.get("username") or "")]

        sresp = client.get("/api/v1/sessions", params={"keyword": keyword, "limit": 50})
        sresp.raise_for_status()
        sessions = sresp.json().get("sessions") or []

        typer.echo(f"contacts (groups): {len(groups)}")
        for c in groups:
            typer.echo(
                f"  username={c.get('username')}  "
                f"nick={c.get('nickname')!r}  remark={c.get('remark')!r}  "
                f"display={c.get('displayName')!r}"
            )
        typer.echo(f"\ncontacts (people): {len(people)}")
        for c in people[:10]:
            typer.echo(
                f"  username={c.get('username')}  "
                f"nick={c.get('nickname')!r}  remark={c.get('remark')!r}"
            )
        typer.echo(f"\nsessions: {len(sessions)}")
        for s in sessions[:20]:
            typer.echo(
                f"  username={s.get('username')}  display={s.get('displayName')!r}  "
                f"type={s.get('sessionType')}"
            )


@weflow_app.command("sessions")
def weflow_sessions(
    keyword: str = typer.Option("", "--keyword", "-k", help="Filter by username/displayName substring"),
    limit: int = typer.Option(10000, "--limit", "-n"),
    only_groups: bool = typer.Option(False, "--groups-only", help="Only list @chatroom sessions"),
) -> None:
    """List sessions WeFlow knows about. Use this to find the exact name or wxid for WO_GROUPS."""
    from .ingest.live import _build_client
    with _build_client() as client:
        params: dict[str, str | int] = {"limit": limit}
        if keyword:
            params["keyword"] = keyword
        resp = client.get("/api/v1/sessions", params=params)
        resp.raise_for_status()
        sessions = resp.json().get("sessions", []) or []
        if only_groups:
            sessions = [s for s in sessions if "@chatroom" in (s.get("username") or "")]
        typer.echo(f"{len(sessions)} sessions:")
        for s in sessions:
            display = s.get("displayName") or "?"
            kind = s.get("sessionType") or "?"
            user = s.get("username") or "?"
            typer.echo(f"  {kind:8s}  {display!r:40s}  {user}")


@openclaw_app.command("mcp-test")
def openclaw_mcp_test() -> None:
    """End-to-end smoke test: spawn `mcp-serve` as a subprocess, do the MCP
    initialize handshake over stdio, list tools, then call search_group_messages
    on the busiest group. This is what OpenClaw will do under the hood - if
    THIS works and OpenClaw still doesn't see the tools, the bug is on
    OpenClaw's side (registration/profile/restart), not ours.
    """
    import asyncio
    import json as _json
    import os
    import sys
    from mcp import ClientSession  # type: ignore[import-untyped]
    from mcp.client.stdio import StdioServerParameters, stdio_client  # type: ignore[import-untyped]

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")

    # Resolve a real group_id BEFORE we start the async session, so database
    # access errors surface with a normal traceback instead of getting buried
    # inside an asyncio TaskGroup ExceptionGroup.
    typer.echo("[0/4] open SQLite ...")
    try:
        with get_conn() as conn:
            db_row = conn.execute(
                "SELECT group_id FROM messages WHERE group_id IS NOT NULL "
                "GROUP BY group_id ORDER BY COUNT(*) DESC LIMIT 1"
            ).fetchone()
            img_row = conn.execute(
                "SELECT msg_id, group_id FROM messages "
                "WHERE type='image' AND media_path IS NOT NULL "
                "ORDER BY msg_id DESC LIMIT 1"
            ).fetchone()
    except Exception as e:
        typer.echo(f"      [ERR] cannot open/read DB at {settings.db_path}: {type(e).__name__}: {e}")
        typer.echo("      Check that the MCP server cwd points at this checkout and that the DB is not locked.")
        raise typer.Exit(2)
    test_gid = db_row["group_id"] if db_row else None
    typer.echo(f"      OK; busiest group_id={test_gid!r}")
    if img_row is not None:
        typer.echo(f"      latest image row: msg_id={img_row['msg_id']} group_id={img_row['group_id']!r}")

    async def run() -> None:
        # mcp.client.stdio does NOT inherit parent env by default, so pass it
        # through explicitly for PATH/HOME/etc. used by the spawned process.
        mcp_command = _self_command("openclaw", "mcp-serve")
        params = StdioServerParameters(
            command=mcp_command[0],
            args=mcp_command[1:],
            env=dict(os.environ),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                typer.echo("[1/4] initialize ...")
                init = await session.initialize()
                typer.echo(f"      server: {init.serverInfo.name} v{init.serverInfo.version}")
                typer.echo(f"      protocol: {init.protocolVersion}")

                typer.echo("[2/4] list tools (with raw schemas) ...")
                tools_resp = await session.list_tools()
                typer.echo(f"      got {len(tools_resp.tools)} tools.")
                for t in tools_resp.tools:
                    in_schema = getattr(t, "inputSchema", None)
                    out_schema = getattr(t, "outputSchema", None)
                    typer.echo(f"      -- {t.name} --")
                    in_str = _json.dumps(in_schema, ensure_ascii=False) if in_schema else "(none)"
                    out_str = _json.dumps(out_schema, ensure_ascii=False) if out_schema else "(none)"
                    typer.echo(f"        inputSchema:  {in_str[:300]}")
                    typer.echo(f"        outputSchema: {out_str[:300]}")

                if test_gid is None:
                    typer.echo("[3/4] skip search_group_messages call - no groups in DB")
                else:
                    typer.echo(f"[3/4] search_group_messages(group_id={test_gid!r}, query='', limit=2) ...")
                    result = await session.call_tool(
                        "search_group_messages",
                        {"group_id": test_gid, "query": "", "limit": 2},
                    )
                    for c in result.content:
                        text = getattr(c, "text", None)
                        if text:
                            typer.echo("      " + text.replace("\n", "\n      ")[:300])

                if img_row is None:
                    typer.echo("[4/4] skip load_image - no image rows with media_path in DB")
                else:
                    typer.echo(
                        f"[4/4] load_image(group_id={img_row['group_id']!r}, "
                        f"msg_id={img_row['msg_id']}) ..."
                    )
                    img_result = await session.call_tool(
                        "load_image",
                        {"group_id": img_row["group_id"], "msg_id": int(img_row["msg_id"])},
                    )
                    typer.echo(f"      isError={getattr(img_result, 'isError', None)}")
                    typer.echo(f"      content blocks: {len(img_result.content)}")
                    for i, c in enumerate(img_result.content):
                        kind = type(c).__name__
                        bits = []
                        for attr in ("type", "mimeType"):
                            if hasattr(c, attr):
                                bits.append(f"{attr}={getattr(c, attr)!r}")
                        if hasattr(c, "data"):
                            d = c.data
                            bits.append(f"data_len={len(d) if isinstance(d, str) else '?'}")
                        if hasattr(c, "text"):
                            bits.append(f"text={c.text[:80]!r}")
                        typer.echo(f"      content[{i}]: {kind}  " + "  ".join(bits))
        typer.echo("\n[OK] our MCP server works end-to-end via stdio.")

    try:
        asyncio.run(run())
    except BaseExceptionGroup as eg:  # type: ignore[name-defined]  # 3.11+
        typer.echo(f"\n[ERR] ExceptionGroup of {len(eg.exceptions)} sub-exception(s):")
        for i, sub in enumerate(eg.exceptions, 1):
            typer.echo(f"  ({i}) {type(sub).__name__}: {sub}")
            inner = getattr(sub, "exceptions", None)
            if inner:
                for j, deeper in enumerate(inner, 1):
                    typer.echo(f"      .{j} {type(deeper).__name__}: {deeper}")
        raise typer.Exit(2)
    except Exception as e:
        typer.echo(f"\n[ERR] {type(e).__name__}: {e}")
        raise typer.Exit(2)


@openclaw_app.command("mcp-serve")
def openclaw_mcp_serve() -> None:
    """Start the MCP server that exposes WeChat-Oracle tools to OpenClaw's
    wechat-bot agent. Stdio-based - meant to be spawned by OpenClaw's MCP
    client. Register on the OpenClaw side roughly like:

      openclaw mcp set wechat-oracle \\
        --command "uv" --args "run wechat-oracle openclaw mcp-serve"

    A portable build uses ``WeChatOracle.exe openclaw mcp-serve`` instead.

    Exposes the full OpenClaw tool surface: history search, quote/forward
    expansion, media reads, and memory/persona read-write tools.
    """
    from .mcp_server import run_mcp_server
    run_mcp_server()


@openclaw_app.command("ping")
def openclaw_ping(
    message: str = typer.Argument("ping", help="Text to send"),
    timeout: float = typer.Option(120.0, "--timeout", "-t", help="HTTP timeout in seconds"),
) -> None:
    """Smoke-test the OpenClaw chat-completions endpoint with the configured
    agent. Prints HTTP status, latency, and the assistant reply (or error).

    Note: a real wechat-bot agent with full skills loaded can have prompt
    sizes in the 10-50K range. First call typically takes 20-40s. Default
    timeout is 120s; raise with --timeout if your agent is heavier.
    """
    import time as _time
    import sys as _sys
    import httpx
    if not settings.openclaw_token:
        typer.echo("WARNING:  WO_OPENCLAW_TOKEN is empty - set it to the gateway's auth token.")
        raise typer.Exit(1)
    url = f"{settings.openclaw_gateway_url.rstrip('/')}/v1/chat/completions"
    payload = {
        "model": f"openclaw/{settings.openclaw_agent_id}",
        "messages": [{"role": "user", "content": message}],
    }
    headers = {"Authorization": f"Bearer {settings.openclaw_token}"}
    typer.echo(f"POST {url}  model={payload['model']!r}")
    typer.echo(f"(waiting up to {timeout:.0f}s for response - first call to a heavy agent can take 20-40s)")
    _sys.stdout.flush()
    t0 = _time.time()
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    except httpx.HTTPError as e:
        typer.echo(f"[ERR] HTTP error after {_time.time() - t0:.1f}s: {e}")
        raise typer.Exit(2)
    dt = _time.time() - t0
    typer.echo(f"HTTP {resp.status_code}  dt={dt:.2f}s")
    if resp.status_code != 200:
        typer.echo(f"body: {resp.text[:600]}")
        raise typer.Exit(2)
    body = resp.json()
    choices = body.get("choices") or []
    if not choices:
        typer.echo(f"WARNING:  no choices in response: {body}")
        raise typer.Exit(2)
    reply = (choices[0].get("message") or {}).get("content") or ""
    usage = body.get("usage") or {}
    typer.echo(f"reply: {reply!r}")
    if usage:
        typer.echo(f"usage: {usage}")


@app.command("status")
def status() -> None:
    """Quick health check: db row counts."""
    init_db()
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        per_status = conn.execute(
            "SELECT status, COUNT(*) FROM messages GROUP BY status"
        ).fetchall()
        per_group = conn.execute(
            "SELECT group_name, COUNT(*) FROM messages GROUP BY group_name"
        ).fetchall()
    typer.echo(f"db: {settings.db_path}")
    typer.echo(f"total messages: {total}")
    typer.echo("by status: " + ", ".join(f"{r[0]}={r[1]}" for r in per_status))
    typer.echo("by group: " + ", ".join(f"{r[0]}={r[1]}" for r in per_group))


def main() -> None:
    _configure_stdio_utf8()
    app()


if __name__ == "__main__":
    main()

"""Small `.env` editor used by the terminal UI.

The normal runtime still reads configuration through `config.Settings`. This
module handles the operator-facing edit/save path for the initial native
SQLite + OpenAI-compatible runtime, including local group authorization and
automatic-summary switches. Advanced backend fields remain only for source
compatibility and are not exposed by this UI.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlparse

from .config import Settings


ENV_PATH = Path(".env")
AGENT_CONFIG_KEYS = (
    "WO_AGENT_BACKEND",
    "WO_AGENT_BASE_PROBABILITY",
    "WO_AGENT_PROACTIVE_MODE",
    "WO_AGENT_CONTINUATION_ENABLED",
    "WO_AGENT_CONTINUATION_MAX_FOLLOWUPS",
    "WO_AGENT_CONTINUATION_DELAY_SECONDS",
    "WO_AGENT_CONTINUATION_TTL_SECONDS",
    "WO_REPLY_MENTION_POLICY",
    "WO_LLM_MODEL",
    "WO_LLM_ENDPOINT",
    "WO_LLM_API_KEY",
    "WO_GROUPS",
    "WO_REPLY_ALLOWED_GROUPS",
    "WO_RAW_WECHAT_ENABLED",
    "WO_RAW_WECHAT_ACCOUNT",
    "WO_HOURLY_SUMMARY_ENABLED",
    "WO_DAILY_SUMMARY_ENABLED",
    "WO_MEMBER_KB_ENABLED",
    "WO_MEMBER_KB_INTERVAL_SECONDS",
    "WO_MEMBER_KB_CHUNK_CHARS",
    "WO_MEMBER_KB_MAX_CONCURRENCY",
    "WO_MEMBER_KB_RETRIES",
    "WO_OPENCLAW_AGENT_ID",
)


@dataclass(frozen=True)
class AgentRuntimeConfig:
    backend: str
    proactive_mode: str
    llm_model: str
    openclaw_agent_id: str
    agent_base_probability: float = 0.25
    reply_mention_policy: str = "explicit"
    continuation_enabled: bool = True
    continuation_max_followups: int = 2
    continuation_delay_seconds: int = 90
    continuation_ttl_seconds: int = 600
    native_configured: bool = False
    openclaw_token_configured: bool = False
    openclaw_configured: bool = False
    pi_configured: bool = False
    llm_endpoint: str = "https://api.deepseek.com"
    llm_api_key_update: str | None = None
    groups: tuple[str, ...] = ()
    available_groups: tuple[tuple[str, str], ...] = ()
    raw_wechat_enabled: bool = False
    raw_wechat_account: str = ""
    hourly_summary_enabled: bool = False
    daily_summary_enabled: bool = False
    member_kb_enabled: bool = False
    member_kb_interval_seconds: int = 3600
    member_kb_chunk_chars: int = 24_000
    member_kb_max_concurrency: int = 2
    member_kb_retries: int = 3

    def env_updates(self) -> dict[str, str]:
        available_names = dict(self.available_groups)
        return {
            "WO_AGENT_BACKEND": self.backend,
            "WO_AGENT_BASE_PROBABILITY": f"{self.agent_base_probability:g}",
            "WO_AGENT_PROACTIVE_MODE": self.proactive_mode,
            "WO_AGENT_CONTINUATION_ENABLED": "true" if self.continuation_enabled else "false",
            "WO_AGENT_CONTINUATION_MAX_FOLLOWUPS": str(self.continuation_max_followups),
            "WO_AGENT_CONTINUATION_DELAY_SECONDS": str(self.continuation_delay_seconds),
            "WO_AGENT_CONTINUATION_TTL_SECONDS": str(self.continuation_ttl_seconds),
            "WO_REPLY_MENTION_POLICY": self.reply_mention_policy,
            "WO_LLM_MODEL": self.llm_model,
            "WO_LLM_ENDPOINT": self.llm_endpoint,
            "WO_GROUPS": json.dumps(
                [available_names.get(group_id, group_id) for group_id in self.groups],
                ensure_ascii=False,
            ),
            "WO_RAW_WECHAT_ENABLED": "true" if self.raw_wechat_enabled else "false",
            "WO_RAW_WECHAT_ACCOUNT": self.raw_wechat_account,
            "WO_HOURLY_SUMMARY_ENABLED": "true" if self.hourly_summary_enabled else "false",
            "WO_DAILY_SUMMARY_ENABLED": "true" if self.daily_summary_enabled else "false",
            "WO_MEMBER_KB_ENABLED": "true" if self.member_kb_enabled else "false",
            "WO_MEMBER_KB_INTERVAL_SECONDS": str(self.member_kb_interval_seconds),
            "WO_MEMBER_KB_CHUNK_CHARS": str(self.member_kb_chunk_chars),
            "WO_MEMBER_KB_MAX_CONCURRENCY": str(self.member_kb_max_concurrency),
            "WO_MEMBER_KB_RETRIES": str(self.member_kb_retries),
            "WO_OPENCLAW_AGENT_ID": self.openclaw_agent_id,
        }


def load_agent_runtime_config() -> AgentRuntimeConfig:
    import shutil
    current = Settings()
    available_groups = _load_authorized_groups(current)
    selected_groups = tuple(
        group_id
        for group_id, display_name in available_groups
        if group_id in current.groups or display_name in current.groups
    )
    if not selected_groups:
        selected_groups = tuple(current.groups)
    return AgentRuntimeConfig(
        backend=(current.agent_backend or "native").lower(),
        proactive_mode=current.agent_proactive_mode,
        llm_model=current.llm_model,
        openclaw_agent_id=current.openclaw_agent_id,
        agent_base_probability=current.agent_base_probability,
        reply_mention_policy=current.reply_mention_policy,
        continuation_enabled=current.agent_continuation_enabled,
        continuation_max_followups=current.agent_continuation_max_followups,
        continuation_delay_seconds=current.agent_continuation_delay_seconds,
        continuation_ttl_seconds=current.agent_continuation_ttl_seconds,
        native_configured=bool(current.llm_api_key),
        openclaw_token_configured=bool(current.openclaw_token),
        openclaw_configured=bool(current.openclaw_token and current.openclaw_agent_id),
        pi_configured=bool(shutil.which(current.pi_executable)),
        llm_endpoint=current.llm_endpoint,
        groups=selected_groups,
        available_groups=available_groups,
        raw_wechat_enabled=current.raw_wechat_enabled,
        raw_wechat_account=current.raw_wechat_account,
        hourly_summary_enabled=current.hourly_summary_enabled,
        daily_summary_enabled=current.daily_summary_enabled,
        member_kb_enabled=current.member_kb_enabled,
        member_kb_interval_seconds=current.member_kb_interval_seconds,
        member_kb_chunk_chars=current.member_kb_chunk_chars,
        member_kb_max_concurrency=current.member_kb_max_concurrency,
        member_kb_retries=current.member_kb_retries,
    )


def save_agent_runtime_config(
    config: AgentRuntimeConfig,
    *,
    env_path: Path = ENV_PATH,
) -> dict[str, str]:
    updates = _validated_updates(config)
    _update_env_file(env_path, updates)
    return updates


def _validated_updates(config: AgentRuntimeConfig) -> dict[str, str]:
    backend = config.backend.strip().lower()
    proactive_mode = config.proactive_mode.strip().lower()
    llm_model = config.llm_model.strip()
    llm_endpoint = config.llm_endpoint.strip()
    openclaw_agent_id = config.openclaw_agent_id.strip()
    probability = float(config.agent_base_probability)
    mention_policy = config.reply_mention_policy.strip().lower()
    continuation_max = int(config.continuation_max_followups)
    continuation_delay = int(config.continuation_delay_seconds)
    continuation_ttl = int(config.continuation_ttl_seconds)
    member_kb_interval = int(config.member_kb_interval_seconds)
    member_kb_chunk_chars = int(config.member_kb_chunk_chars)
    member_kb_concurrency = int(config.member_kb_max_concurrency)
    member_kb_retries = int(config.member_kb_retries)
    groups = tuple(dict.fromkeys(item.strip() for item in config.groups if item.strip()))
    if backend not in {"native", "openclaw", "pi"}:
        raise ValueError("后端只能是 native、openclaw 或 pi")
    if not 0.0 <= probability <= 1.0:
        raise ValueError("触发概率必须在 0 到 1 之间")
    if proactive_mode not in {"off", "reactive", "proactive"}:
        raise ValueError("主动模式只能是 off、reactive 或 proactive")
    if mention_policy not in {"always", "explicit", "never"}:
        raise ValueError("@ 策略只能是 always、explicit 或 never")
    if continuation_max < 0:
        raise ValueError("continuation max_followups must be >= 0")
    if continuation_delay < 5:
        raise ValueError("continuation delay must be >= 5 seconds")
    if continuation_ttl < continuation_delay:
        raise ValueError("continuation TTL must be >= delay")
    if member_kb_interval < 300:
        raise ValueError("member knowledge interval must be at least 300 seconds")
    if not 4_000 <= member_kb_chunk_chars <= 200_000:
        raise ValueError("member knowledge chunk chars must be between 4000 and 200000")
    if not 1 <= member_kb_concurrency <= 2:
        raise ValueError("member knowledge concurrency must be between 1 and 2")
    if not 1 <= member_kb_retries <= 3:
        raise ValueError("member knowledge retries must be between 1 and 3")
    if not llm_model:
        raise ValueError("Native 模型不能为空")
    parsed_endpoint = urlparse(llm_endpoint)
    if parsed_endpoint.scheme not in {"http", "https"} or not parsed_endpoint.netloc:
        raise ValueError("模型 API 地址必须是完整的 http(s) URL")
    if any("\r" in value or "\n" in value for value in (llm_model, llm_endpoint, config.raw_wechat_account)):
        raise ValueError("配置值不能包含换行")
    if backend == "openclaw" and not openclaw_agent_id:
        raise ValueError("OpenClaw Agent ID 不能为空")
    available_names = {group_id: name for group_id, name in config.available_groups}
    updates = {
        "WO_AGENT_BACKEND": backend,
        "WO_AGENT_BASE_PROBABILITY": f"{probability:g}",
        "WO_AGENT_PROACTIVE_MODE": proactive_mode,
        "WO_AGENT_CONTINUATION_ENABLED": "true" if config.continuation_enabled else "false",
        "WO_AGENT_CONTINUATION_MAX_FOLLOWUPS": str(continuation_max),
        "WO_AGENT_CONTINUATION_DELAY_SECONDS": str(continuation_delay),
        "WO_AGENT_CONTINUATION_TTL_SECONDS": str(continuation_ttl),
        "WO_REPLY_MENTION_POLICY": mention_policy,
        "WO_LLM_MODEL": llm_model,
        "WO_LLM_ENDPOINT": llm_endpoint,
        "WO_GROUPS": json.dumps(
            [available_names.get(group_id, group_id) for group_id in groups],
            ensure_ascii=False,
        ),
        "WO_REPLY_ALLOWED_GROUPS": json.dumps(
            [available_names[group_id] for group_id in groups if group_id in available_names],
            ensure_ascii=False,
        ),
        "WO_RAW_WECHAT_ENABLED": "true" if config.raw_wechat_enabled else "false",
        "WO_RAW_WECHAT_ACCOUNT": config.raw_wechat_account.strip().lower(),
        "WO_HOURLY_SUMMARY_ENABLED": "true" if config.hourly_summary_enabled else "false",
        "WO_DAILY_SUMMARY_ENABLED": "true" if config.daily_summary_enabled else "false",
        "WO_MEMBER_KB_ENABLED": "true" if config.member_kb_enabled else "false",
        "WO_MEMBER_KB_INTERVAL_SECONDS": str(member_kb_interval),
        "WO_MEMBER_KB_CHUNK_CHARS": str(member_kb_chunk_chars),
        "WO_MEMBER_KB_MAX_CONCURRENCY": str(member_kb_concurrency),
        "WO_MEMBER_KB_RETRIES": str(member_kb_retries),
        "WO_OPENCLAW_AGENT_ID": openclaw_agent_id,
    }
    if config.llm_api_key_update is not None:
        secret = config.llm_api_key_update.strip()
        if "\r" in secret or "\n" in secret:
            raise ValueError("API key 不能包含换行")
        updates["WO_LLM_API_KEY"] = secret
    return updates


def _update_env_file(path: Path, updates: dict[str, str]) -> None:
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    else:
        lines = [
            "# Generated by `wechat-oracle run`.",
            "# Edit values here or from the terminal UI.",
            "",
        ]
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        key = _active_env_key(line)
        if key is None or key not in updates:
            out.append(line)
            continue
        if key in seen:
            continue
        out.append(f"{key}={updates[key]}")
        seen.add(key)
    missing = [key for key in AGENT_CONFIG_KEYS if key in updates and key not in seen]
    if missing and out and out[-1].strip():
        out.append("")
    for key in missing:
        out.append(f"{key}={updates[key]}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
    try:
        temporary.write_text("\n".join(out) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _active_env_key(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    return stripped.split("=", 1)[0].strip()


def _load_authorized_groups(current: Settings) -> tuple[tuple[str, str], ...]:
    if not current.db_path.is_file():
        return ()
    uri = f"file:{current.db_path.resolve().as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        rows = conn.execute(
            """
            SELECT canonical_group_id, display_name
              FROM raw_group_authorizations
             WHERE enabled=1
             ORDER BY display_name, canonical_group_id
            """
        ).fetchall()
    except sqlite3.Error:
        return ()
    finally:
        if "conn" in locals():
            conn.close()
    return tuple((str(row[0]), str(row[1])) for row in rows)

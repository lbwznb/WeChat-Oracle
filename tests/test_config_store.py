from pathlib import Path

import pytest

from wechat_oracle.config_store import AgentRuntimeConfig, save_agent_runtime_config


def _config(**changes) -> AgentRuntimeConfig:
    values = dict(
        backend="native",
        proactive_mode="reactive",
        llm_model="model-a",
        llm_endpoint="https://api.example.test/v1",
        openclaw_agent_id="unused",
        native_configured=True,
        available_groups=(("123@chatroom", "测试群"),),
        groups=("123@chatroom",),
        raw_wechat_enabled=True,
        raw_wechat_account="0123456789ab",
        hourly_summary_enabled=True,
        daily_summary_enabled=True,
        member_kb_enabled=True,
    )
    values.update(changes)
    return AgentRuntimeConfig(**values)


def test_save_runtime_config_preserves_secret_when_blank(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("WO_LLM_API_KEY=keep-me\nUNRELATED=yes\n", encoding="utf-8")
    save_agent_runtime_config(_config(llm_api_key_update=None), env_path=env)
    text = env.read_text(encoding="utf-8")
    assert "WO_LLM_API_KEY=keep-me" in text
    assert "UNRELATED=yes" in text
    assert 'WO_GROUPS=["测试群"]' in text
    assert 'WO_REPLY_ALLOWED_GROUPS=["测试群"]' in text


def test_save_runtime_config_updates_secret_without_echoing_it(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    updates = save_agent_runtime_config(
        _config(llm_api_key_update="new-secret"), env_path=env
    )
    assert updates["WO_LLM_API_KEY"] == "new-secret"
    assert "WO_LLM_API_KEY=new-secret" in env.read_text(encoding="utf-8")


def test_config_rejects_newline_injection(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="换行"):
        save_agent_runtime_config(
            _config(llm_endpoint="https://api.example.test\nWO_REPLY=false"),
            env_path=tmp_path / ".env",
        )


def test_native_config_does_not_require_legacy_openclaw_id(tmp_path: Path) -> None:
    updates = save_agent_runtime_config(
        _config(openclaw_agent_id=""), env_path=tmp_path / ".env"
    )
    assert updates["WO_AGENT_BACKEND"] == "native"


def test_member_kb_config_is_persisted(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    updates = save_agent_runtime_config(
        _config(
            member_kb_enabled=True,
            member_kb_interval_seconds=7200,
            member_kb_chunk_chars=32000,
            member_kb_max_concurrency=2,
            member_kb_retries=3,
        ),
        env_path=env,
    )
    assert updates["WO_MEMBER_KB_ENABLED"] == "true"
    assert updates["WO_MEMBER_KB_INTERVAL_SECONDS"] == "7200"
    assert updates["WO_MEMBER_KB_CHUNK_CHARS"] == "32000"
    assert updates["WO_MEMBER_KB_MAX_CONCURRENCY"] == "2"
    assert updates["WO_MEMBER_KB_RETRIES"] == "3"


@pytest.mark.parametrize(
    "changes",
    [
        {"member_kb_interval_seconds": 299},
        {"member_kb_chunk_chars": 3999},
        {"member_kb_max_concurrency": 0},
        {"member_kb_max_concurrency": 3},
        {"member_kb_retries": 0},
        {"member_kb_retries": 4},
    ],
)
def test_member_kb_config_rejects_unsafe_bounds(tmp_path: Path, changes: dict) -> None:
    with pytest.raises(ValueError, match="member knowledge"):
        save_agent_runtime_config(_config(**changes), env_path=tmp_path / ".env")

import asyncio

from textual.app import App

from wechat_oracle.config_store import AgentRuntimeConfig
from wechat_oracle.db import get_conn, init_db
from wechat_oracle.run_tui import ConfigScreen


class ConfigHost(App):
    def __init__(self, config: AgentRuntimeConfig):
        super().__init__()
        self.config = config
        self.result = None

    def on_mount(self) -> None:
        self.push_screen(ConfigScreen(self.config), self._saved)

    def _saved(self, result) -> None:
        self.result = result


def test_member_kb_first_enable_requires_explicit_consent(tmp_path, monkeypatch) -> None:
    path = tmp_path / "archive.db"
    init_db(path)
    with get_conn(path) as conn:
        conn.execute(
            """
            INSERT INTO messages
                (group_id,group_name,t,type,sender_wxid,sender_display,
                 content_text,source,status,dedupe_key)
            VALUES ('g','测试群',1,'text','u','甲','hello','backfill','raw','tui-one')
            """
        )
    monkeypatch.setattr(
        "wechat_oracle.run_tui.get_conn", lambda *args, **kwargs: get_conn(path)
    )
    config = AgentRuntimeConfig(
        backend="native",
        proactive_mode="reactive",
        llm_model="model",
        llm_endpoint="https://api.example.test/v1",
        openclaw_agent_id="",
        native_configured=True,
        groups=("g",),
        available_groups=(("g", "测试群"),),
        member_kb_enabled=False,
    )
    app = ConfigHost(config)

    async def scenario() -> None:
        async with app.run_test(size=(120, 50)) as pilot:
            await pilot.click("#config-menu-member-kb")
            assert app.screen.query_one("#member-kb-consent-confirm")
            await pilot.click("#member-kb-consent-confirm")
            await pilot.pause()
            await pilot.click("#config-menu-save")
            await pilot.pause()

    asyncio.run(scenario())
    assert app.result is not None
    assert app.result.member_kb_enabled is True

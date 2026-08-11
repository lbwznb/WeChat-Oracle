import time
from pathlib import Path

from wechat_oracle.agent.backend import AgentChatOutcome
from wechat_oracle.config import settings
from wechat_oracle.db import get_conn, init_db
from wechat_oracle.dispatcher import _claim, _process
from wechat_oracle.ingest.writer import write_messages
from wechat_oracle.models import Message, MsgType


class UnusedLLM:
    name = "unused"


class FakeAgent:
    name = "fake-agent"

    def chat(self, *, ctx, user_question, trigger_kind, reflection_enabled=None):
        assert ctx.group_name == "人心黄黄"
        assert ctx.requester is None  # wx4py UIA cannot expose the sender
        assert user_question == "你好"
        assert trigger_kind == "mention"
        return AgentChatOutcome(reply_text="你好，已收到。", trace_block="fake trace")


class RecordingReplier:
    def __init__(self):
        self.sent = []

    def send(self, group_name, requester, text):
        self.sent.append((group_name, requester, text))

    def disconnect(self):
        pass


def test_unknown_sender_ui_mention_reaches_agent_and_reply(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "e2e.db"
    init_db(db_path)
    monkeypatch.setattr(settings, "bot_name", "小助理")
    monkeypatch.setattr(settings, "agent_proactive_mode", "off")
    monkeypatch.setattr("wechat_oracle.dispatcher.get_agent_backend", lambda: FakeAgent())
    monkeypatch.setattr("wechat_oracle.dispatcher.append_event", lambda *a, **k: None)
    monkeypatch.setattr("wechat_oracle.dispatcher.append_log", lambda *a, **k: None)
    monkeypatch.setattr("wechat_oracle.dispatcher._terminal_print", lambda *a, **k: None)

    replier = RecordingReplier()
    with get_conn(db_path) as conn:
        write_messages(conn, [Message(
            wx_msg_id="ui-live:test",
            group_id="ui:test-group",
            group_name="人心黄黄",
            sender_display=None,
            t=int(time.time()),
            type=MsgType.TEXT,
            content_text="@小助理 你好",
            source="live",
        )])
        row = conn.execute(
            "SELECT msg_id, group_id, group_name, t, type, content_text, transcript, "
            "sender_display, sender_wxid, quote_text, reply_to_wx_msg_id, wx_msg_id "
            "FROM messages"
        ).fetchone()
        assert _claim(conn, row["msg_id"])
        _process(
            conn, UnusedLLM(), replier, row,
            log_path=tmp_path / "dispatcher.log", llm_log_path=None,
        )
        run = conn.execute(
            "SELECT status, result FROM command_runs WHERE msg_id=?", (row["msg_id"],)
        ).fetchone()

    assert run["status"] == "ok"
    assert replier.sent == [("人心黄黄", None, "你好，已收到。")]

from __future__ import annotations

import json
from pathlib import Path

from wechat_oracle.agent.tools_read import ReadMemberProfileTool, SearchMemberProfilesTool
from wechat_oracle.config import settings
from wechat_oracle.daily_summary import run_summary_group
from wechat_oracle.db import get_conn, init_db
from wechat_oracle.member_knowledge import update_member_profile_section
from wechat_oracle.time_ranges import SummaryPeriod


class _CaptureLLM:
    name = "capture"

    def __init__(self) -> None:
        self.users: list[str] = []

    def complete_text(self, *, model, system, user, temperature=0.3, max_tokens=None):
        self.users.append(user)
        return "summary"


class _Replier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, group_name, requester, text):
        self.sent.append(text)


def _message(conn, *, group_id: str, wxid: str | None, name: str, t: int, key: str, text: str) -> None:
    conn.execute(
        """
        INSERT INTO messages
            (group_id, group_name, sender_wxid, sender_display, t, type,
             content_text, source, dedupe_key)
        VALUES (?, ?, ?, ?, ?, 'text', ?, 'live', ?)
        """,
        (group_id, group_id, wxid, name, t, text, key),
    )


def test_member_tools_are_group_scoped_and_do_not_personalize_unknown(tmp_path: Path) -> None:
    db_path = tmp_path / "members.db"
    init_db(db_path)
    with get_conn(db_path) as conn:
        _message(conn, group_id="g1", wxid="u1", name="Alice", t=100, key="g1-a", text="hello")
        _message(conn, group_id="g2", wxid="u1", name="Alice", t=100, key="g2-a", text="other group")
        update_member_profile_section(conn, "g1", "u1", "identity", "g1 profile")
        update_member_profile_section(conn, "g2", "u1", "identity", "g2 profile")

        g1_profile = json.loads(ReadMemberProfileTool(conn, "g1").call({"sender_wxid": "u1"}))
        assert g1_profile["status"] == "ok"
        assert g1_profile["sections"]["identity"] == "g1 profile"

        g2_profile = json.loads(ReadMemberProfileTool(conn, "g2").call({"sender_wxid": "u1"}))
        assert g2_profile["sections"]["identity"] == "g2 profile"

        unknown = json.loads(ReadMemberProfileTool(conn, "g1").call({"sender_wxid": "UNKNOWN"}))
        assert unknown["status"] == "not_found"
        assert "profile" not in unknown

        only_g1 = json.loads(SearchMemberProfilesTool(conn, "g1").call({"query": "g2 profile"}))
        assert only_g1["profiles"] == []


def test_summary_context_contains_only_active_participants_and_is_background(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "summary-members.db"
    init_db(db_path)
    monkeypatch.setattr(settings, "bot_name", "assistant")
    llm = _CaptureLLM()
    period = SummaryPeriod("hourly", 100, 200, "period 100-200")
    with get_conn(db_path) as conn:
        _message(conn, group_id="g1", wxid="u1", name="Alice", t=150, key="active", text="current event")
        _message(conn, group_id="g1", wxid="u2", name="Bob", t=90, key="old", text="old event")
        _message(conn, group_id="g1", wxid="u3", name="Carol", t=200, key="boundary", text="boundary event")
        update_member_profile_section(conn, "g1", "u1", "identity", "active profile")
        update_member_profile_section(conn, "g1", "u2", "identity", "inactive profile")
        update_member_profile_section(conn, "g1", "u3", "identity", "boundary profile")

        result = run_summary_group(
            conn,
            group_id="g1",
            group_name="g1",
            period=period,
            min_messages=1,
            replier=_Replier(),
            llm=llm,
            sleep=lambda _: None,
        )
        assert result == "sent"

    assert len(llm.users) == 1
    prompt = llm.users[0]
    assert "current event" in prompt
    assert "active profile" in prompt
    assert "inactive profile" not in prompt
    assert "boundary profile" not in prompt
    assert "Active member background context" in prompt
    assert "raw chat messages in the requested period are the event source" in prompt
    assert "old profile claims or evidence as events that happened in this period" in prompt

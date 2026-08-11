import sqlite3

from wechat_oracle.dispatcher import Candidate, SumCommand, fetch_candidates, parse_command, summarize_chat_hierarchical


class FakeLLM:
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def complete_text(self, *, model, system, user, temperature=0.3, max_tokens=None):
        self.calls.append(user)
        return f"摘要{len(self.calls)}"


class OversizedFakeLLM(FakeLLM):
    def complete_text(self, *, model, system, user, temperature=0.3, max_tokens=None):
        self.calls.append(user)
        return "长" * 100


def test_natural_summary_routes_to_sum() -> None:
    command = parse_command("@小助理 总结一下昨天聊了什么", "小助理")
    assert isinstance(command, SumCommand)
    assert command.since_t is not None
    assert command.until_t is not None


def test_hierarchical_summary_chunks_and_merges() -> None:
    llm = FakeLLM()
    candidates = [Candidate(f"m:{i}", i, "群友", "内容" * 10, None) for i in range(5)]
    result = summarize_chat_hierarchical(
        llm, "fake", candidates, "", chunk_messages=2, chunk_chars=10_000
    )
    assert result.startswith("摘要")
    assert len(llm.calls) == 4  # three leaves + one merge


def test_oversized_partial_summaries_still_reduce() -> None:
    llm = OversizedFakeLLM()
    candidates = [Candidate(f"m:{i}", i, "群友", "内容", None) for i in range(4)]
    result = summarize_chat_hierarchical(
        llm, "fake", candidates, "", chunk_messages=1, chunk_chars=10
    )
    assert result == "长" * 100
    assert len(llm.calls) < 10


def test_ui_messages_with_unknown_sender_remain_summary_candidates() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE messages (
            msg_id INTEGER PRIMARY KEY, group_id TEXT, group_name TEXT, t INTEGER,
            type TEXT, sender_wxid TEXT, sender_display TEXT, content_text TEXT,
            transcript TEXT, quote_text TEXT, reply_to_wx_msg_id TEXT, wx_msg_id TEXT
        );
        CREATE TABLE forwarded_records (
            id INTEGER PRIMARY KEY, parent_msg_id INTEGER, t INTEGER,
            sender_display TEXT, content TEXT
        );
        CREATE TABLE group_aliases (
            alias_id TEXT PRIMARY KEY, canonical_group_id TEXT NOT NULL
        );
        INSERT INTO messages
            (msg_id, group_id, group_name, t, type, content_text)
        VALUES (1, 'ui:g1', '人心黄黄', 1, 'text', '可见消息');
        """
    )
    rows = fetch_candidates(
        conn, "ui:g1", target=None, since_t=None, limit=None, bot_name="小助理"
    )
    assert [row.content for row in rows] == ["可见消息"]

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from wechat_oracle.config import settings
from wechat_oracle.daily_summary import resolve_daily_groups
from wechat_oracle.db import get_conn, init_db, transaction
from wechat_oracle.dispatcher import fetch_candidates
from wechat_oracle.ingest.group_identity import (
    canonical_group_id,
    register_group_alias,
    ui_group_id,
)
from wechat_oracle.ingest.writer import write_messages
from wechat_oracle.models import Message, MsgType


def test_alias_unifies_ui_and_raw_group_for_daily_summary(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "archive.db"
    init_db(db_path)
    group_name = "测试群"
    canonical = "456@chatroom"
    alias = ui_group_id(group_name)
    monkeypatch.setattr(settings, "groups", [group_name])
    monkeypatch.setattr(settings, "reply_allowed_groups", [group_name])
    monkeypatch.setattr(settings, "raw_wechat_enabled", False)
    with get_conn(db_path) as conn:
        with transaction(conn):
            register_group_alias(conn, group_name=group_name, canonical_id=canonical)
        assert canonical_group_id(conn, group_name) == canonical
        conn.executemany(
            """
            INSERT INTO messages(group_id,group_name,t,type,content_text,source,status,dedupe_key)
            VALUES (?,?,1,'text',?,'backfill','raw',?)
            """,
            [
                (alias, group_name, "legacy ui", "legacy-ui"),
                (canonical, group_name, "raw", "raw"),
            ],
        )
        assert resolve_daily_groups(conn) == [(canonical, group_name)]
        candidates = fetch_candidates(
            conn,
            group_id=canonical,
            target=None,
            since_t=None,
            limit=None,
            bot_name="",
        )
        assert [item.content for item in candidates] == ["legacy ui", "raw"]


def test_raw_summary_resolution_requires_current_account_authorization(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "archive.db"
    init_db(db_path)
    group_name = "测试群"
    canonical = "456@chatroom"
    monkeypatch.setattr(settings, "groups", [canonical])
    monkeypatch.setattr(settings, "reply_allowed_groups", [group_name])
    monkeypatch.setattr(settings, "raw_wechat_enabled", True)
    monkeypatch.setattr(settings, "raw_wechat_account", "0123456789ab")
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO messages(group_id,group_name,t,type,content_text,source,status,dedupe_key)
            VALUES (?,?,1,'text','消息','backfill','raw','raw-one')
            """,
            (canonical, group_name),
        )
        assert resolve_daily_groups(conn) == []
        conn.execute(
            """
            INSERT INTO raw_group_authorizations
                (account_fingerprint, canonical_group_id, display_name,
                 contact_generation, enabled, created_at, updated_at)
            VALUES (?, ?, ?, 'v1', 1, 1, 1)
            """,
            (settings.raw_wechat_account, canonical, group_name),
        )
        assert resolve_daily_groups(conn) == [(canonical, group_name)]


def test_raw_identity_upgrades_one_exact_ui_live_row(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    init_db(db_path)
    group_id = "456@chatroom"
    live = Message(
        wx_msg_id="ui-live:event-1",
        group_id=group_id,
        group_name="测试群",
        t=int(datetime(2026, 8, 11, tzinfo=ZoneInfo("Asia/Hong_Kong")).timestamp()),
        type=MsgType.TEXT,
        content_text="同一条消息",
        source="live",
    )
    raw = Message(
        wx_msg_id="9001",
        group_id=group_id,
        group_name="测试群",
        sender_wxid="wxid_a",
        sender_display="阿甲",
        t=live.t,
        type=MsgType.TEXT,
        content_text=live.content_text,
        source="backfill",
    )
    with get_conn(db_path) as conn:
        assert write_messages(conn, [live]) == (1, 1)
        assert write_messages(conn, [raw]) == (1, 0)
        assert write_messages(conn, [raw]) == (1, 0)
        rows = conn.execute(
            "SELECT wx_msg_id,sender_wxid,sender_display,source,dedupe_key FROM messages"
        ).fetchall()
        assert len(rows) == 1
        assert tuple(rows[0]) == (
            "9001", "wxid_a", "阿甲", "live", raw.compute_dedupe_key(),
        )

from pathlib import Path

from typer.testing import CliRunner

from wechat_oracle import cli
from wechat_oracle.cli import app
from wechat_oracle.db import get_conn, init_db
from wechat_oracle.member_knowledge import update_member_profile_section


runner = CliRunner()


def _archive(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "archive.db"
    init_db(path)
    with get_conn(path) as conn:
        conn.execute(
            """
            INSERT INTO messages
                (group_id,group_name,t,type,sender_wxid,sender_display,
                 content_text,source,status,dedupe_key)
            VALUES ('g1','群一',1,'text','wx-a','甲','SECRET-RAW-LINE',
                    'backfill','raw','member-cli-one')
            """
        )
        update_member_profile_section(
            conn, "g1", "wx-a", "interests", "喜欢数据库", locked=True
        )
    monkeypatch.setattr(cli, "init_db", lambda: None)
    monkeypatch.setattr(cli, "get_conn", lambda *args, **kwargs: get_conn(path))
    monkeypatch.setattr(cli.settings, "groups", ["g1"])
    monkeypatch.setattr(cli.settings, "raw_wechat_enabled", False)
    return path


def test_show_does_not_dump_raw_messages_without_explicit_flag(
    tmp_path: Path, monkeypatch
) -> None:
    _archive(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        ["member-kb", "show", "--group-id", "g1", "--member", "wx-a"],
    )
    assert result.exit_code == 0, result.output
    assert "喜欢数据库" in result.output
    assert "SECRET-RAW-LINE" not in result.output

    explicit = runner.invoke(
        app,
        [
            "member-kb", "show", "--group-id", "g1", "--member", "wx-a",
            "--messages", "--limit", "10",
        ],
    )
    assert explicit.exit_code == 0, explicit.output
    assert "SECRET-RAW-LINE" in explicit.output


def test_selected_groups_resolve_display_name_to_exact_group_id(
    tmp_path: Path, monkeypatch
) -> None:
    path = _archive(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.settings, "groups", ["群一"])
    monkeypatch.setattr(cli.settings, "raw_wechat_enabled", False)
    with get_conn(path) as conn:
        assert cli._member_kb_selected_group_ids(conn) == ["g1"]


def test_show_rejects_an_unselected_group(tmp_path: Path, monkeypatch) -> None:
    _archive(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.settings, "groups", ["different-group"])
    result = runner.invoke(
        app,
        ["member-kb", "show", "--group-id", "g1", "--member", "wx-a"],
    )
    assert result.exit_code == 2
    assert "Invalid value" in result.output


def test_delete_requires_confirmation_and_keeps_messages(
    tmp_path: Path, monkeypatch
) -> None:
    path = _archive(tmp_path, monkeypatch)
    refused = runner.invoke(
        app,
        ["member-kb", "delete", "--group-id", "g1", "--member", "wx-a"],
    )
    assert refused.exit_code != 0

    deleted = runner.invoke(
        app,
        [
            "member-kb", "delete", "--group-id", "g1", "--member", "wx-a",
            "--yes",
        ],
    )
    assert deleted.exit_code == 0, deleted.output
    with get_conn(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM member_profiles WHERE deleted_at IS NULL"
        ).fetchone()[0] == 0

    monkeypatch.setattr(
        "wechat_oracle.member_knowledge.run_member_update",
        lambda *args, **kwargs: {"status": "succeeded", "processed_messages": 1},
    )
    monkeypatch.setattr(cli, "_member_kb_llm", lambda: object())
    rebuilt = runner.invoke(
        app,
        [
            "member-kb", "rebuild", "--group-id", "g1", "--member", "wx-a",
            "--yes",
        ],
    )
    assert rebuilt.exit_code == 0, rebuilt.output
    with get_conn(path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM member_profiles WHERE deleted_at IS NULL"
        ).fetchone()[0] == 1

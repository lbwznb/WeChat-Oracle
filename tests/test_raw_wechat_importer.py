import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from experimental.raw_wechat.importer import (
    import_group_text_messages,
    import_group_text_messages_many,
    import_group_text_messages_many_with_cursors,
    message_table,
)
from wechat_oracle.db import get_conn, init_db


def test_importer_resolves_group_maps_sender_and_dedupes(tmp_path: Path) -> None:
    contact = tmp_path / "contact.db"
    message = tmp_path / "message.db"
    group_id = "123@chatroom"
    table = message_table(group_id)
    with sqlite3.connect(contact) as conn:
        conn.executescript("""
        CREATE TABLE contact (
          id INTEGER, username TEXT, local_type INTEGER, alias TEXT,
          encrypt_username TEXT, flag INTEGER, delete_flag INTEGER,
          verify_flag INTEGER, remark TEXT, remark_quan_pin TEXT,
          remark_pin_yin_initial TEXT, nick_name TEXT, pin_yin_initial TEXT,
          quan_pin TEXT, big_head_url TEXT, small_head_url TEXT,
          head_img_md5 TEXT, chat_room_notify INTEGER, is_in_chat_room INTEGER,
          description TEXT, extra_buffer BLOB, chat_room_type INTEGER
        );
        """)
        conn.executemany(
            "INSERT INTO contact(username,remark,nick_name,is_in_chat_room) VALUES (?,?,?,?)",
            [(group_id, "", "人心黄黄", 1), ("wxid_a", "阿甲", "甲", 0)],
        )
    with sqlite3.connect(message) as conn:
        conn.execute("CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY, is_session INTEGER)")
        conn.execute("INSERT INTO Name2Id(rowid,user_name,is_session) VALUES (7,'wxid_a',0)")
        conn.execute(f'''CREATE TABLE "{table}" (
          local_id INTEGER PRIMARY KEY, server_id INTEGER, local_type INTEGER,
          sort_seq INTEGER, real_sender_id INTEGER, create_time INTEGER,
          status INTEGER, upload_status INTEGER, download_status INTEGER,
          server_seq INTEGER, origin_source INTEGER, source TEXT,
          message_content TEXT, compress_content TEXT, packed_info_data BLOB,
          WCDB_CT_message_content INTEGER DEFAULT NULL,
          WCDB_CT_source INTEGER DEFAULT NULL
        )''')
        conn.executemany(
            f'''INSERT INTO "{table}"
            (local_id,server_id,local_type,real_sender_id,create_time,origin_source,message_content)
            VALUES (?,?,?,?,?,?,?)''',
            [(1, 101, 1, 7, 1000, 2, "你好"), (2, 102, 1, 0, 1001, 4, "收到")],
        )
    archive = tmp_path / "archive.db"
    init_db(archive)
    with get_conn(archive) as conn:
        resolved, attempted, inserted = import_group_text_messages(
            conn, message, contact, group_name="人心黄黄",
        )
        assert (resolved, attempted, inserted) == (group_id, 2, 2)
        assert import_group_text_messages(conn, message, contact, group_name="人心黄黄")[1:] == (2, 0)
        rows = conn.execute(
            "SELECT sender_wxid,sender_display,content_text FROM messages ORDER BY t"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("wxid_a", "阿甲", "你好"), (None, "我", "收到"),
        ]


def test_importer_combines_shards_and_skips_missing_group_table(tmp_path: Path) -> None:
    contact = tmp_path / "contact.db"
    first = tmp_path / "message_0.db"
    second = tmp_path / "message_3.db"
    group_id = "456@chatroom"
    table = message_table(group_id)
    with sqlite3.connect(contact) as conn:
        conn.executescript("""
        CREATE TABLE contact (
          username TEXT, alias TEXT, remark TEXT, nick_name TEXT,
          is_in_chat_room INTEGER
        );
        """)
        conn.executemany(
            "INSERT INTO contact(username,alias,remark,nick_name,is_in_chat_room) VALUES (?,?,?,?,?)",
            [(group_id, "", "", "测试群", 1), ("wxid_b", "", "小乙", "乙", 0)],
        )
    with sqlite3.connect(first) as conn:
        conn.execute("CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY, is_session INTEGER)")
    with sqlite3.connect(second) as conn:
        conn.execute("CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY, is_session INTEGER)")
        conn.execute("INSERT INTO Name2Id(rowid,user_name,is_session) VALUES (8,'wxid_b',0)")
        conn.execute(f'''CREATE TABLE "{table}" (
          local_id INTEGER PRIMARY KEY, server_id INTEGER, local_type INTEGER,
          sort_seq INTEGER, real_sender_id INTEGER, create_time INTEGER,
          status INTEGER, upload_status INTEGER, download_status INTEGER,
          server_seq INTEGER, origin_source INTEGER, source TEXT,
          message_content TEXT, compress_content TEXT, packed_info_data BLOB,
          WCDB_CT_message_content INTEGER DEFAULT NULL,
          WCDB_CT_source INTEGER DEFAULT NULL
        )''')
        conn.execute(
            f'''INSERT INTO "{table}"
            (local_id,server_id,local_type,real_sender_id,create_time,origin_source,message_content)
            VALUES (1,0,1,8,2000,2,'新消息')'''
        )

    archive = tmp_path / "archive.db"
    init_db(archive)
    with get_conn(archive) as conn:
        result = import_group_text_messages_many(
            conn, [first, second], contact, group_name="测试群",
        )
        assert result == (group_id, 1, 1)
        row = conn.execute("SELECT wx_msg_id,sender_display,content_text FROM messages").fetchone()
        assert tuple(row) == ("local:3:1", "小乙", "新消息")
        _, attempted, inserted, cursors = import_group_text_messages_many_with_cursors(
            conn, [first, second], contact, group_name="测试群",
        )
        assert (attempted, inserted, cursors) == (1, 0, {"3": 1})
        assert import_group_text_messages_many_with_cursors(
            conn,
            [first, second],
            contact,
            group_name="测试群",
            after_local_ids=cursors,
        )[1:] == (0, 0, {"3": 1})

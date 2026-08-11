from datetime import datetime

from wechat_oracle.ingest.ui_live import _history_timestamp, ui_group_id


def test_ui_group_id_is_stable_and_namespaced() -> None:
    assert ui_group_id("人心黄黄") == ui_group_id("人心黄黄")
    assert ui_group_id("人心黄黄").startswith("ui:")
    assert ui_group_id("另一个群") != ui_group_id("人心黄黄")


def test_history_timestamp_understands_yesterday() -> None:
    now = datetime(2026, 8, 11, 15, 30)
    value = _history_timestamp("昨天 23:15", now)
    assert datetime.fromtimestamp(value) == datetime(2026, 8, 10, 23, 15)

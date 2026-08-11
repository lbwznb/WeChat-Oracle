from datetime import datetime
from zoneinfo import ZoneInfo

from wechat_oracle.time_ranges import parse_natural_time_range, previous_natural_day


TZ = ZoneInfo("Asia/Hong_Kong")
NOW = datetime(2026, 8, 11, 15, 30, tzinfo=TZ)


def test_yesterday_is_half_open_natural_day() -> None:
    result = parse_natural_time_range("昨天聊了什么", now=NOW)
    assert result is not None
    assert datetime.fromtimestamp(result.start_t, TZ) == datetime(2026, 8, 10, tzinfo=TZ)
    assert datetime.fromtimestamp(result.end_t, TZ) == datetime(2026, 8, 11, tzinfo=TZ)
    assert result.remaining_text == "聊了什么"


def test_recent_hours_preserves_topic() -> None:
    result = parse_natural_time_range("帮我看最近 3 小时的装修讨论", now=NOW)
    assert result is not None
    assert result.end_t - result.start_t == 3 * 3600
    assert "装修讨论" in result.remaining_text


def test_explicit_date_range_includes_end_date() -> None:
    result = parse_natural_time_range("2026-08-01 到 2026-08-03 项目进度", now=NOW)
    assert result is not None
    assert result.end_t - result.start_t == 3 * 86400
    assert result.remaining_text == "项目进度"


def test_previous_day() -> None:
    result = previous_natural_day(now=NOW)
    assert result.label == "2026-08-10"
    assert result.end_t - result.start_t == 86400


def test_invalid_calendar_date_is_rejected() -> None:
    assert parse_natural_time_range("总结 2026-02-30", now=NOW) is None

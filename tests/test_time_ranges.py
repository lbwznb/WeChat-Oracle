from datetime import datetime
from zoneinfo import ZoneInfo

from wechat_oracle.time_ranges import (
    latest_mature_summary_periods,
    parse_natural_time_range,
    previous_natural_day,
)


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


def test_hourly_summary_waits_for_grace_and_is_exactly_one_hour() -> None:
    before = latest_mature_summary_periods(
        now=datetime(2026, 8, 11, 16, 4, 59, tzinfo=TZ),
        tz=TZ,
        grace_seconds=300,
        hourly=True,
    )[0]
    mature = latest_mature_summary_periods(
        now=datetime(2026, 8, 11, 16, 5, 0, tzinfo=TZ),
        tz=TZ,
        grace_seconds=300,
        hourly=True,
    )[0]
    assert datetime.fromtimestamp(before.end_t, TZ).hour == 15
    assert datetime.fromtimestamp(mature.end_t, TZ).hour == 16
    assert mature.end_t - mature.start_t == 3600


def test_dst_hour_is_absolute_and_natural_day_uses_local_midnights() -> None:
    ny = ZoneInfo("America/New_York")
    spring = latest_mature_summary_periods(
        now=datetime(2026, 3, 8, 3, 5, tzinfo=ny),
        tz=ny,
        grace_seconds=0,
        hourly=True,
        daily=True,
    )
    assert spring[0].end_t - spring[0].start_t == 3600
    day = previous_natural_day(now=datetime(2026, 3, 9, 1, 0, tzinfo=ny), tz=ny)
    assert day.end_t - day.start_t == 23 * 3600

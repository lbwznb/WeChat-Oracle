"""Deterministic Chinese natural-language time ranges for chat summaries."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

DEFAULT_TZ = ZoneInfo("Asia/Hong_Kong")


@dataclass(frozen=True)
class TimeRange:
    start_t: int
    end_t: int
    label: str
    remaining_text: str


SummaryKind = Literal["hourly", "daily"]


@dataclass(frozen=True)
class SummaryPeriod:
    kind: SummaryKind
    start_t: int
    end_t: int
    label: str


def latest_mature_summary_periods(
    *,
    now: datetime | None = None,
    tz: ZoneInfo = DEFAULT_TZ,
    grace_seconds: int = 300,
    hourly: bool = False,
    daily: bool = False,
) -> tuple[SummaryPeriod, ...]:
    """Return the latest completed periods after the ingest grace window.

    Hour windows are absolute 3600-second ranges so DST gaps/folds cannot
    create zero- or two-hour jobs. Day windows use adjacent local midnights.
    """
    if grace_seconds < 0:
        raise ValueError("grace_seconds must be non-negative")
    current = _as_local(now, tz)
    cutoff = datetime.fromtimestamp(current.timestamp() - grace_seconds, tz)
    periods: list[SummaryPeriod] = []
    if hourly:
        hour_end = cutoff.replace(minute=0, second=0, microsecond=0)
        end_t = int(hour_end.timestamp())
        start_t = end_t - 3600
        start = datetime.fromtimestamp(start_t, tz)
        end = datetime.fromtimestamp(end_t, tz)
        label = f"{start:%Y-%m-%d %H:%M%z} - {end:%H:%M%z}"
        periods.append(SummaryPeriod("hourly", start_t, end_t, label))
    if daily:
        day_end = datetime.combine(cutoff.date(), time.min, tzinfo=tz)
        day_start = datetime.combine(cutoff.date() - timedelta(days=1), time.min, tzinfo=tz)
        periods.append(
            SummaryPeriod(
                "daily",
                int(day_start.timestamp()),
                int(day_end.timestamp()),
                f"{day_start:%Y-%m-%d}",
            )
        )
    return tuple(periods)


def parse_natural_time_range(
    text: str,
    *,
    now: datetime | None = None,
    tz: ZoneInfo = DEFAULT_TZ,
) -> TimeRange | None:
    """Extract one supported time phrase and return a half-open range."""
    if now is None:
        current = datetime.now(tz)
    elif now.tzinfo is None:
        current = now.replace(tzinfo=tz)
    else:
        current = now.astimezone(tz)
    day_start = current.replace(hour=0, minute=0, second=0, microsecond=0)

    relative_days = {"今天": 0, "昨天": 1, "前天": 2}
    for word, delta in relative_days.items():
        match = re.search(word, text)
        if match:
            start = day_start - timedelta(days=delta)
            end = start + timedelta(days=1)
            return _result(text, match.span(), start, end, word)

    match = re.search(r"最近\s*(\d+)\s*(分钟|小时|天)", text)
    if match:
        count = int(match.group(1))
        if count <= 0:
            return None
        units = {"分钟": "minutes", "小时": "hours", "天": "days"}
        start = current - timedelta(**{units[match.group(2)]: count})
        return _result(text, match.span(), start, current, match.group(0).replace(" ", ""))

    date_pat = r"(\d{4})[-年/](\d{1,2})[-月/](\d{1,2})日?"
    match = re.search(date_pat + r"\s*(?:到|至|~|—|–)\s*" + date_pat, text)
    if match:
        try:
            start = _local_date(match.group(1), match.group(2), match.group(3), tz)
            last = _local_date(match.group(4), match.group(5), match.group(6), tz)
        except ValueError:
            return None
        if last < start:
            return None
        end = last + timedelta(days=1)
        return _result(text, match.span(), start, end, f"{start:%Y-%m-%d} 至 {last:%Y-%m-%d}")

    match = re.search(date_pat, text)
    if match:
        try:
            start = _local_date(match.group(1), match.group(2), match.group(3), tz)
        except ValueError:
            return None
        return _result(text, match.span(), start, start + timedelta(days=1), f"{start:%Y-%m-%d}")
    return None


def previous_natural_day(
    *, now: datetime | None = None, tz: ZoneInfo = DEFAULT_TZ
) -> TimeRange:
    current = _as_local(now, tz)
    end = datetime.combine(current.date(), time.min, tzinfo=tz)
    start = datetime.combine(current.date() - timedelta(days=1), time.min, tzinfo=tz)
    return TimeRange(int(start.timestamp()), int(end.timestamp()), f"{start:%Y-%m-%d}", "")


def _as_local(now: datetime | None, tz: ZoneInfo) -> datetime:
    if now is None:
        return datetime.now(tz)
    if now.tzinfo is None:
        return now.replace(tzinfo=tz)
    return now.astimezone(tz)


def _local_date(year: str, month: str, day: str, tz: ZoneInfo) -> datetime:
    return datetime(int(year), int(month), int(day), tzinfo=tz)


def _result(
    source: str,
    span: tuple[int, int],
    start: datetime,
    end: datetime,
    label: str,
) -> TimeRange:
    remaining = (source[:span[0]] + " " + source[span[1]:]).strip()
    remaining = re.sub(r"\s+", " ", remaining)
    return TimeRange(int(start.timestamp()), int(end.timestamp()), label, remaining)

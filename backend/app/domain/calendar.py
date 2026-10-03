"""근무 달력 계산. app 내부 모듈을 import하지 않는 순수 함수다.

근무 구간은 Horizon 원점 기준 정수 분 [lo, hi)의 목록이고, 로더가 정렬·비겹침·비맞닿음을 보장한다.
작업 [start, end)는 근무 구간 하나 안에 있어야 한다(기본 제약 CALENDAR).
"""

import re
from collections.abc import Sequence
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

Intervals = Sequence[tuple[int, int]] | Sequence[Sequence[int]]


def fits_work_interval(start: int, end: int, work_intervals: Intervals) -> bool:
    """[start, end)가 근무 구간 하나 안에 있는가. Rule Engine CALENDAR와 같은 판정이다."""
    return any(lo <= start and end <= hi for lo, hi in work_intervals)


def start_domain(duration: int, work_intervals: Intervals) -> list[list[int]]:
    """근무 구간 안에 들어가는 시작 시각의 구간 목록 [[lo, hi − duration], …] (CP-SAT 도메인용)."""
    return [[lo, hi - duration] for lo, hi in work_intervals if hi - duration >= lo]


def has_work_slot(
    earliest_start: int,
    latest_start: int,
    latest_end: int,
    duration: int,
    work_intervals: Intervals,
) -> bool:
    """시간창 안에 근무 구간 하나에 들어가는 시작 시각이 하나라도 있는가 (폼 검사)."""
    return any(
        max(earliest_start, lo) <= min(latest_start, hi - duration, latest_end - duration)
        for lo, hi in work_intervals
    )


def work_minutes(start: int, end: int, work_intervals: Intervals) -> int:
    """[start, end)와 근무 구간이 겹치는 분의 합."""
    return sum(max(0, min(end, hi) - max(start, lo)) for lo, hi in work_intervals)


def work_delay(base_start: int, start: int, work_intervals: Intervals) -> int:
    """근무 분 지연: 기준 시작에서 새 시작까지의 근무 분(늦어질 때만). 저장하지 않고 조회 시 계산한다.

    달력 분 지연은 max(0, start − base_start)다.
    """
    return work_minutes(base_start, start, work_intervals) if start > base_start else 0


WEEKDAYS = "월화수목금토일"


def now_view(
    now: datetime, horizon_start_utc: str, timezone: str, horizon_minutes: int
) -> dict[str, str | int]:
    """현장의 지금: 현장 날짜·요일·시각, Horizon 원점 기준 분, Horizon 안(IN)·앞(BEFORE)·뒤(AFTER).

    알려 주기만 하는 값이다. 판정·계산에는 쓰지 않는다 (CV-14).
    """
    origin = datetime.fromisoformat(horizon_start_utc)
    minute = int((now - origin).total_seconds() // 60)
    local = now.astimezone(ZoneInfo(timezone))
    horizon = "BEFORE" if minute < 0 else "IN" if minute < horizon_minutes else "AFTER"
    return {
        "local": f"{local:%Y-%m-%d}({WEEKDAYS[local.weekday()]}) {local:%H:%M}",
        "minute": minute,
        "horizon": horizon,
    }


SITE_TIME = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:\((.)\))? (\d{2}):(\d{2})$")


def site_time(horizon_start_utc: str, timezone: str, minute: int) -> str:
    """Horizon 원점 기준 분 → 현장 날짜·시각 "YYYY-MM-DD(요일) HH:MM". Agent 도구의 시각 인자와 같은 형식이다."""
    origin = datetime.fromisoformat(horizon_start_utc).astimezone(ZoneInfo(timezone))
    t = origin + timedelta(minutes=minute)
    return f"{t:%Y-%m-%d}({WEEKDAYS[t.weekday()]}) {t:%H:%M}"


def parse_site_time(text: str, horizon_start_utc: str, timezone: str, horizon_minutes: int) -> int:
    """현장 날짜·시각 "YYYY-MM-DD HH:MM"(요일 "(화)"는 붙여도 된다) → Horizon 원점 기준 분 (AG-21).

    형식이 틀리거나, 없는 날짜이거나, 요일이 날짜와 다르거나, Horizon 밖이면 ValueError.
    """
    m = SITE_TIME.match(text.strip())
    if m is None:
        raise ValueError(f"not a site time: {text!r}")
    year, month, day, weekday, hour, minute = m.groups()
    local = datetime(
        int(year), int(month), int(day), int(hour), int(minute), tzinfo=ZoneInfo(timezone)
    )
    if weekday is not None and weekday != WEEKDAYS[local.weekday()]:
        raise ValueError(f"weekday does not match the date: {text!r}")
    value = int((local - datetime.fromisoformat(horizon_start_utc)).total_seconds() // 60)
    if not 0 <= value <= horizon_minutes:
        raise ValueError(f"outside the horizon: {text!r}")
    return value


def local_clock(horizon_start_utc: str, timezone: str, minute: int) -> str:
    """Horizon 원점 기준 분 → 현장 시각 "MM/DD(요일) HH:MM" (분 변환 실수를 알아보게)."""
    origin = datetime.fromisoformat(horizon_start_utc).astimezone(ZoneInfo(timezone))
    t = origin + timedelta(minutes=minute)
    return f"{t:%m/%d}({WEEKDAYS[t.weekday()]}) {t:%H:%M}"

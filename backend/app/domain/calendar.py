"""근무 달력 계산. app 내부 모듈을 import하지 않는 순수 함수다.

근무 구간은 Horizon 원점 기준 정수 분 [lo, hi)의 목록이고, 로더가 정렬·비겹침·비맞닿음을 보장한다.
작업 [start, end)는 근무 구간 하나 안에 있어야 한다(기본 제약 CALENDAR).
"""

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


def local_clock(horizon_start_utc: str, timezone: str, minute: int) -> str:
    """Horizon 원점 기준 분 → 현장 시각 "MM/DD(요일) HH:MM" (분 변환 실수를 알아보게)."""
    origin = datetime.fromisoformat(horizon_start_utc).astimezone(ZoneInfo(timezone))
    t = origin + timedelta(minutes=minute)
    return f"{t:%m/%d}({WEEKDAYS[t.weekday()]}) {t:%H:%M}"

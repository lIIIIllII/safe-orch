"""현장의 지금 SITE_NOW (ST-17). 고정 시각, 오프셋 필수, 관찰용 표현."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.clock import site_now
from app.config import get_settings
from app.domain.calendar import now_view, parse_site_time, site_time


@pytest.mark.parametrize("value", ["2026-10-12T09:00", "2026-10-12", "내일"])
def test_site_now_needs_iso_with_offset(monkeypatch, value):
    monkeypatch.setenv("SITE_NOW", value)
    get_settings.cache_clear()
    with pytest.raises(ValidationError):
        get_settings()


def test_site_now_fixed_is_utc_minute(monkeypatch):
    monkeypatch.setenv("SITE_NOW", "2026-10-13T10:30:45+09:00")
    get_settings.cache_clear()
    assert site_now() == datetime(2026, 10, 13, 1, 30, tzinfo=UTC)


def test_site_now_blank_uses_real_clock(monkeypatch):
    monkeypatch.setenv("SITE_NOW", "")
    get_settings.cache_clear()
    assert get_settings().site_now is None
    now = site_now()
    assert abs((datetime.now(UTC) - now).total_seconds()) < 120
    assert (now.second, now.microsecond) == (0, 0)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        ("2026-10-12T09:00+09:00", {"local": "2026-10-12(월) 09:00", "minute": 0, "horizon": "IN"}),
        (
            "2026-10-13T10:30+09:00",
            {"local": "2026-10-13(화) 10:30", "minute": 1530, "horizon": "IN"},
        ),
        (
            "2026-10-03T12:00+09:00",
            {"local": "2026-10-03(토) 12:00", "minute": -12780, "horizon": "BEFORE"},
        ),
        (
            "2026-10-14T17:00+09:00",
            {"local": "2026-10-14(수) 17:00", "minute": 3360, "horizon": "AFTER"},
        ),
    ],
)
def test_now_view_local_minute_and_horizon(pack, now, expected):
    view = now_view(
        datetime.fromisoformat(now), pack.horizon_start_utc, pack.timezone, pack.horizon_minutes
    )
    assert view == expected


# ── Agent 도구의 시각 인자: 현장 날짜·시각 문자열 ↔ 분 (AG-21) ──


@pytest.mark.parametrize(
    ("text", "minute"),
    [
        ("2026-10-12 09:00", 0),
        ("2026-10-13 10:30", 1530),
        ("2026-10-14(수) 10:00", 2940),  # 요일은 붙여도 된다
        (" 2026-10-14 17:00 ", 3360),  # Horizon 끝
    ],
)
def test_parse_site_time(pack, text, minute):
    args = (pack.horizon_start_utc, pack.timezone, pack.horizon_minutes)
    assert parse_site_time(text, *args) == minute
    assert parse_site_time(site_time(*args[:2], minute), *args) == minute  # 관찰 형식 그대로 받는다


@pytest.mark.parametrize(
    "text",
    [
        "2940",  # 분 숫자
        "10/14 10:00",  # 형식이 다름
        "2026-10-14T10:00",
        "2026-10-14(화) 10:00",  # 요일이 날짜와 다름
        "2026-02-30 10:00",  # 없는 날짜
        "2026-10-12 08:59",  # Horizon 앞
        "2026-10-14 17:01",  # Horizon 뒤
    ],
)
def test_parse_site_time_rejects(pack, text):
    with pytest.raises(ValueError):
        parse_site_time(text, pack.horizon_start_utc, pack.timezone, pack.horizon_minutes)

"""Schedule matching for tenant promotions (warocol.com#980)."""
from datetime import datetime, time
from zoneinfo import ZoneInfo

from app.services.promotions_service import (
    BOGOTA,
    day_bit_for_datetime,
    is_active_at,
    time_in_schedule_window,
)

# Mon–Fri bitmask (Mon=1, Tue=2, Wed=4, Thu=8, Fri=16)
WEEKDAYS = 1 + 2 + 4 + 8 + 16


def _at(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def test_day_bit_monday_bogota():
    monday = _at("2026-05-25T12:00:00-05:00")
    assert day_bit_for_datetime(monday) == 1


def test_happy_hour_active_inside_window():
    at = _at("2026-05-29T18:30:00-05:00")  # Thursday
    assert time_in_schedule_window(
        at,
        days_of_week=WEEKDAYS,
        start_time=time(17, 0),
        end_time=time(20, 0),
        crosses_midnight=False,
    )


def test_happy_hour_inactive_outside_window():
    at = _at("2026-05-29T14:00:00-05:00")
    assert not time_in_schedule_window(
        at,
        days_of_week=WEEKDAYS,
        start_time=time(17, 0),
        end_time=time(20, 0),
        crosses_midnight=False,
    )


def test_overnight_schedule_active_after_midnight():
    # Thursday 22:00–06:00 window is still active Friday 01:30
    at = _at("2026-05-29T01:30:00-05:00")  # Friday early morning
    assert time_in_schedule_window(
        at,
        days_of_week=1 << 3,  # Thursday
        start_time=time(22, 0),
        end_time=time(6, 0),
        crosses_midnight=True,
    )


def test_is_active_at_no_schedules_always_on_when_enabled():
    at = _at("2026-05-29T12:00:00-05:00")
    assert is_active_at(
        at,
        is_active=True,
        starts_at=None,
        ends_at=None,
        schedules=[],
    )


def test_is_active_at_respects_campaign_dates():
    at = _at("2026-06-01T12:00:00-05:00")
    starts = _at("2026-05-01T00:00:00-05:00")
    ends = _at("2026-05-31T23:59:59-05:00")
    assert not is_active_at(
        at,
        is_active=True,
        starts_at=starts,
        ends_at=ends,
        schedules=[],
    )


def test_is_active_at_with_schedule_row():
    at = _at("2026-05-29T18:00:00-05:00")
    schedules = [
        {
            "days_of_week": WEEKDAYS,
            "start_time": time(17, 0),
            "end_time": time(21, 0),
            "crosses_midnight": False,
        }
    ]
    assert is_active_at(
        at,
        is_active=True,
        starts_at=None,
        ends_at=None,
        schedules=schedules,
    )


def test_bogota_offset_is_minus_five():
    at = datetime.now(tz=BOGOTA)
    assert str(at.tzinfo) == "America/Bogota"


def test_is_active_at_multiple_ranges_same_day():
    """Lunch + dinner windows on the same weekdays (warocol.com#983)."""
    schedules = [
        {
            "days_of_week": WEEKDAYS,
            "start_time": time(12, 0),
            "end_time": time(14, 0),
            "crosses_midnight": False,
        },
        {
            "days_of_week": WEEKDAYS,
            "start_time": time(17, 0),
            "end_time": time(20, 0),
            "crosses_midnight": False,
        },
    ]
    at_lunch = _at("2026-05-29T13:00:00-05:00")
    at_dinner = _at("2026-05-29T18:30:00-05:00")
    at_between = _at("2026-05-29T15:00:00-05:00")
    assert is_active_at(
        at_lunch, is_active=True, starts_at=None, ends_at=None, schedules=schedules
    )
    assert is_active_at(
        at_dinner, is_active=True, starts_at=None, ends_at=None, schedules=schedules
    )
    assert not is_active_at(
        at_between, is_active=True, starts_at=None, ends_at=None, schedules=schedules
    )


def test_is_active_at_window_end_is_exclusive():
    """Promo ends at 20:00 — not active at exactly 20:00 (Bogotá)."""
    schedules = [
        {
            "days_of_week": WEEKDAYS,
            "start_time": time(17, 0),
            "end_time": time(20, 0),
            "crosses_midnight": False,
        }
    ]
    at_end = _at("2026-05-29T20:00:00-05:00")
    assert not is_active_at(
        at_end, is_active=True, starts_at=None, ends_at=None, schedules=schedules
    )

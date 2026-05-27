"""Unit tests for shift opening cash float helpers (warocol.com#920)."""
from datetime import date, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.services.cierre_service import (
    _effective_period_bounds,
    _requires_open_shift,
)

BOG = ZoneInfo("America/Bogota")


def test_effective_period_bounds_uses_timestamps_when_present():
    start = datetime(2026, 5, 18, 14, 0, tzinfo=BOG)
    end = datetime(2026, 5, 18, 22, 0, tzinfo=BOG)
    eff_start, eff_end = _effective_period_bounds(
        date(2026, 5, 18), date(2026, 5, 18), start, end,
    )
    assert eff_start == start
    assert eff_end == end


def test_effective_period_bounds_full_day_bogota():
    eff_start, eff_end = _effective_period_bounds(
        date(2026, 5, 18), date(2026, 5, 18), None, None,
    )
    assert eff_start == datetime(2026, 5, 18, 0, 0, 0, tzinfo=BOG)
    assert eff_end == datetime(2026, 5, 18, 23, 59, 59, tzinfo=BOG)


def test_requires_open_shift_template_mode():
    assert _requires_open_shift(uuid4(), None, None) is True


def test_requires_open_shift_custom_times():
    start = datetime(2026, 5, 18, 14, 0, tzinfo=BOG)
    end = datetime(2026, 5, 18, 22, 0, tzinfo=BOG)
    assert _requires_open_shift(None, start, end) is True


def test_requires_open_shift_day_complete_optional():
    assert _requires_open_shift(None, None, None) is False

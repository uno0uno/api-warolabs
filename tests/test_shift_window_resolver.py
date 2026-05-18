"""Unit tests for shift template window resolution (warocol.com#684)."""
from datetime import date, time
from uuid import uuid4

from app.services.shift_window_service import resolve_shift_template_window


def test_overnight_template_may_18_bogota():
    """AC: 22:00–06:00 on May 18 → start May 18 22:00, end May 19 06:00."""
    data = resolve_shift_template_window(
        anchor_date=date(2026, 5, 18),
        start_time=time(22, 0),
        end_time=time(6, 0),
        crosses_midnight=True,
    )
    assert data["periodStart"] == "2026-05-18"
    assert data["periodEnd"] == "2026-05-19"
    assert data["periodStartTime"] == "2026-05-18T22:00:00-05:00"
    assert data["periodEndTime"] == "2026-05-19T06:00:00-05:00"
    assert data["crossesMidnight"] is True


def test_same_day_template():
    data = resolve_shift_template_window(
        anchor_date=date(2026, 5, 18),
        start_time=time(6, 0),
        end_time=time(14, 0),
        crosses_midnight=False,
    )
    assert data["periodStart"] == "2026-05-18"
    assert data["periodEnd"] == "2026-05-18"
    assert data["periodStartTime"] == "2026-05-18T06:00:00-05:00"
    assert data["periodEndTime"] == "2026-05-18T14:00:00-05:00"
    assert data["crossesMidnight"] is False


def test_includes_template_metadata_when_provided():
    tid = uuid4()
    data = resolve_shift_template_window(
        anchor_date=date(2026, 5, 18),
        start_time=time(6, 0),
        end_time=time(14, 0),
        crosses_midnight=False,
        template_id=tid,
        template_name="Mañana",
    )
    assert data["templateId"] == str(tid)
    assert data["templateName"] == "Mañana"

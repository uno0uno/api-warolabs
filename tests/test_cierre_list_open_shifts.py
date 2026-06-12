"""Tests for open shifts in cierre list (cash_shift_openings)."""
from datetime import date, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.services.cierre_service import _open_shift_list_row_to_dict, _row_to_dict

BOG = ZoneInfo("America/Bogota")


def _mock_closed_row(**overrides):
    template_id = overrides.pop("shift_template_id", None)
    template_name = overrides.pop("shift_template_name", None)
    base = {
        "id": uuid4(),
        "accounting_period_id": uuid4(),
        "tenant_id": uuid4(),
        "period_start": date(2026, 5, 18),
        "period_end": date(2026, 5, 18),
        "period_start_time": None,
        "period_end_time": None,
        "closed_at": datetime(2026, 5, 18, 14, 30, tzinfo=BOG),
        "shift_template_id": template_id,
        "shift_template_name": template_name,
        "total_sales": 100.0,
        "items_sold": 5,
        "total_tips": 0.0,
        "total_tip_tax": 0.0,
        "cash_tips": 0.0,
        "total_cash": 80.0,
        "total_card": 10.0,
        "total_digital": 10.0,
        "total_credit": 0.0,
        "gastos_efectivo": 0.0,
        "opening_cash": 0.0,
        "cash_expected": 80.0,
        "cash_counted": 80.0,
        "cash_difference": 0.0,
        "cash_left_in_drawer": None,
        "notes": None,
    }
    base.update(overrides)
    return base


def _mock_open_row(**overrides):
    base = {
        "id": uuid4(),
        "opening_cash": 301100.0,
        "opening_breakdown": None,
        "opened_at": datetime(2026, 6, 11, 13, 5, tzinfo=BOG),
        "opened_by_user_id": uuid4(),
        "shift_template_id": uuid4(),
        "period_start": date(2026, 6, 11),
        "period_end": date(2026, 6, 12),
        "period_start_time": datetime(2026, 6, 11, 12, 0, tzinfo=BOG),
        "period_end_time": datetime(2026, 6, 12, 4, 1, tzinfo=BOG),
        "shift_template_name": "Tarde",
    }
    base.update(overrides)
    return base


def test_open_shift_list_row_to_dict_status_and_template():
    data = _open_shift_list_row_to_dict(_mock_open_row())
    assert data["status"] == "open"
    assert data["shiftTemplateName"] == "Tarde"
    assert data["openingCash"] == 301100.0
    assert data["openedAt"] is not None
    assert data["totalSales"] is None
    assert data["cashDifference"] is None
    assert data["closedAt"] is None


def test_open_shift_list_row_day_only():
    data = _open_shift_list_row_to_dict(
        _mock_open_row(
            shift_template_id=None,
            shift_template_name=None,
            period_start_time=None,
            period_end_time=None,
            period_end=date(2026, 6, 10),
        )
    )
    assert data["status"] == "open"
    assert data["shiftTemplateId"] is None
    assert data["shiftTemplateName"] is None


def test_row_to_dict_includes_closed_status():
    data = _row_to_dict(_mock_closed_row())
    assert data["status"] == "closed"
    assert data["totalSales"] == 100.0
    assert data["closedAt"] is not None

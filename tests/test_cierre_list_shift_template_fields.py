"""Tests for cierre list/detail shift template fields (warocol.com#687)."""
from datetime import date, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.services.cierre_service import _row_to_dict

BOG = ZoneInfo("America/Bogota")


def _mock_row(**overrides):
    template_id = overrides.pop("shift_template_id", None)
    template_name = overrides.pop("shift_template_name", None)
    base = {
        "id": uuid4(),
        "accounting_period_id": uuid4(),
        "tenant_id": uuid4(),
        "period_start": date(2026, 5, 18),
        "period_end": date(2026, 5, 18),
        "period_start_time": datetime(2026, 5, 18, 6, 0, tzinfo=BOG),
        "period_end_time": datetime(2026, 5, 18, 14, 0, tzinfo=BOG),
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


def test_row_to_dict_includes_shift_template_fields():
    tid = uuid4()
    data = _row_to_dict(_mock_row(shift_template_id=tid, shift_template_name="Mañana"))
    assert data["status"] == "closed"
    assert data["shiftTemplateId"] == str(tid)
    assert data["shiftTemplateName"] == "Mañana"
    assert data["periodStartTime"] is not None


def test_row_to_dict_legacy_null_template():
    data = _row_to_dict(_mock_row(shift_template_id=None, shift_template_name=None))
    assert data["shiftTemplateId"] is None
    assert data["shiftTemplateName"] is None


def test_row_to_dict_custom_window_without_template():
    data = _row_to_dict(
        _mock_row(
            shift_template_id=None,
            shift_template_name=None,
            period_start_time=datetime(2026, 5, 18, 15, 0, tzinfo=BOG),
            period_end_time=datetime(2026, 5, 18, 18, 0, tzinfo=BOG),
        )
    )
    assert data["shiftTemplateId"] is None
    assert data["periodStartTime"] is not None

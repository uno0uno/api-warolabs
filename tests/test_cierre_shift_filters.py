"""Unit tests for cierre shift-window filter builders (issue #685)."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.services.cierre_service import (
    _build_expense_filter,
    _build_open_tables_filter,
    _build_order_date_filter,
)

BOG = ZoneInfo("America/Bogota")


def test_order_filter_date_only_uses_bogota_dates():
    sql, params = _build_order_date_filter(
        date(2026, 5, 18), date(2026, 5, 18), None, None
    )
    assert "AT TIME ZONE 'America/Bogota'" in sql
    assert params == [date(2026, 5, 18), date(2026, 5, 18)]


def test_order_filter_shift_uses_timestamps():
    start = datetime(2026, 5, 18, 14, 0, tzinfo=BOG)
    end = datetime(2026, 5, 18, 22, 0, tzinfo=BOG)
    sql, params = _build_order_date_filter(
        date(2026, 5, 18), date(2026, 5, 18), start, end
    )
    assert "order_date >=" in sql
    assert "AT TIME ZONE" not in sql
    assert params == [start, end]


def test_expense_filter_date_only_uses_transaction_date():
    sql, params = _build_expense_filter(
        date(2026, 5, 18), date(2026, 5, 18), None, None
    )
    assert "transaction_date" in sql
    assert "created_at" not in sql
    assert params == [date(2026, 5, 18), date(2026, 5, 18)]


def test_expense_filter_shift_uses_created_at():
    start = datetime(2026, 5, 18, 14, 0, tzinfo=BOG)
    end = datetime(2026, 5, 18, 22, 0, tzinfo=BOG)
    sql, params = _build_expense_filter(
        date(2026, 5, 18), date(2026, 5, 18), start, end
    )
    assert "created_at >=" in sql
    assert "transaction_date" not in sql
    assert params == [start, end]


def test_open_tables_filter_date_only_uses_bogota_dates():
    sql, params = _build_open_tables_filter(
        date(2026, 5, 18), date(2026, 5, 18), None, None
    )
    assert "AT TIME ZONE 'America/Bogota'" in sql
    assert "opened_at::date" not in sql
    assert params == [date(2026, 5, 18), date(2026, 5, 18)]


def test_open_tables_filter_shift_uses_opened_at_before_end():
    end = datetime(2026, 5, 18, 22, 0, tzinfo=BOG)
    sql, params = _build_open_tables_filter(
        date(2026, 5, 18), date(2026, 5, 18), datetime(2026, 5, 18, 14, 0, tzinfo=BOG), end
    )
    assert "ts.opened_at <=" in sql
    assert "opened_at::date" not in sql
    assert params == [end]

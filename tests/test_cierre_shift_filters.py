"""Unit tests for cierre shift-window filter builders (issue #685)."""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.services.cierre_service import (
    _compute_preview,
    _build_expense_filter,
    _build_open_tables_filter,
    _build_order_date_filter,
    _build_purchase_payment_filter,
)

BOG = ZoneInfo("America/Bogota")


def test_order_filter_date_only_uses_bogota_dates():
    sql, params = _build_order_date_filter(
        date(2026, 5, 18), date(2026, 5, 18), None, None
    )
    assert "AT TIME ZONE $2" in sql
    assert "MAX(op_close.paid_at)" in sql
    assert params == ["America/Bogota", date(2026, 5, 18), date(2026, 5, 18)]


def test_order_filter_shift_uses_close_or_payment_timestamps():
    start = datetime(2026, 5, 18, 14, 0, tzinfo=BOG)
    end = datetime(2026, 5, 18, 22, 0, tzinfo=BOG)
    sql, params = _build_order_date_filter(
        date(2026, 5, 18), date(2026, 5, 18), start, end
    )
    assert "MAX(op_close.paid_at)" in sql
    assert "order_date" in sql
    assert "AT TIME ZONE" not in sql
    assert params == [start, end]


def test_order_filter_aliased_qualifies_order_id():
    start = datetime(2026, 5, 18, 14, 0, tzinfo=BOG)
    end = datetime(2026, 5, 18, 22, 0, tzinfo=BOG)
    sql, params = _build_order_date_filter(
        date(2026, 5, 18), date(2026, 5, 18), start, end, order_alias="o"
    )
    assert "op_close.order_id = o.id" in sql
    assert "o.order_date" in sql
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


def test_expense_filter_can_qualify_joined_expense_columns():
    start = datetime(2026, 7, 1, 10, 0, tzinfo=BOG)
    end = datetime(2026, 7, 2, 4, 1, tzinfo=BOG)
    sql, params = _build_expense_filter(
        date(2026, 7, 1),
        date(2026, 7, 1),
        start,
        end,
        table_alias="e",
    )

    assert "e.created_at >=" in sql
    assert " e.created_at <=" in sql
    assert "AND created_at" not in sql
    assert params == [start, end]


def test_purchase_payment_filter_can_qualify_joined_purchase_columns():
    start = datetime(2026, 7, 1, 10, 0, tzinfo=BOG)
    end = datetime(2026, 7, 2, 4, 1, tzinfo=BOG)
    sql, params = _build_purchase_payment_filter(
        date(2026, 7, 1),
        date(2026, 7, 1),
        start,
        end,
        table_alias="tp",
    )

    assert "COALESCE(tp.payment_date, tp.paid_at, tp.purchase_date)" in sql
    assert "COALESCE(payment_date" not in sql
    assert params == [start, end]


def test_open_tables_filter_date_only_uses_bogota_dates():
    sql, params = _build_open_tables_filter(
        date(2026, 5, 18), date(2026, 5, 18), None, None
    )
    assert "AT TIME ZONE $2" in sql
    assert "opened_at::date" not in sql
    assert params == ["America/Bogota", date(2026, 5, 18), date(2026, 5, 18)]


def test_open_tables_filter_covers_rebel_rebel_night_opened_at():
    opened_bogota = datetime(2026, 6, 6, 21, 52, tzinfo=BOG)
    opened_utc = opened_bogota.astimezone(timezone.utc)

    assert opened_utc.date() == date(2026, 6, 7)

    sql, params = _build_open_tables_filter(
        date(2026, 6, 6), date(2026, 6, 6), None, None
    )

    assert "(ts.opened_at AT TIME ZONE $2)::date" in sql
    assert "opened_at::date" not in sql
    assert params == ["America/Bogota", date(2026, 6, 6), date(2026, 6, 6)]


def test_open_tables_filter_shift_uses_opened_at_before_end():
    end = datetime(2026, 5, 18, 22, 0, tzinfo=BOG)
    sql, params = _build_open_tables_filter(
        date(2026, 5, 18), date(2026, 5, 18), datetime(2026, 5, 18, 14, 0, tzinfo=BOG), end
    )
    assert "ts.opened_at <=" in sql
    assert "opened_at::date" not in sql
    assert params == [end]


class _PreviewConn:
    def __init__(self, open_tables_count):
        self.open_tables_count = open_tables_count
        self.open_tables_sql = None
        self.open_tables_args = None

    async def fetchrow(self, sql, *args):
        if "FROM table_sessions ts" in sql:
            self.open_tables_sql = sql
            self.open_tables_args = args
            return {"open_tables_count": self.open_tables_count}
        if "AS items_sold" in sql:
            return {"total_sales": 0, "items_sold": 0}
        if "AS total_tips" in sql:
            return {"total_tips": 0, "total_tip_tax": 0}
        if "AS cash_tips" in sql:
            return {"cash_tips": 0}
        if "AS gastos_efectivo" in sql:
            return {"gastos_efectivo": 0}
        if "AS cash_purchases" in sql:
            return {"cash_purchases": 0}
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def fetch(self, *_args):
        return []


@pytest.mark.asyncio
async def test_preview_open_tables_count_excludes_permanent_bar_sessions(monkeypatch):
    async def no_wallet_recharges(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(
        "app.services.customer_wallet_service.fetch_wallet_recharge_totals_for_cierre",
        no_wallet_recharges,
    )
    conn = _PreviewConn(open_tables_count=0)

    preview = await _compute_preview(
        conn,
        tenant_id="tenant-id",
        period_start=date(2026, 6, 6),
        period_end=date(2026, 6, 6),
    )

    assert preview["openTablesCount"] == 0
    assert "JOIN tables t ON t.id = ts.table_id" in conn.open_tables_sql
    assert "AND t.is_bar IS FALSE" in conn.open_tables_sql
    assert conn.open_tables_args == ("tenant-id", "America/Bogota", date(2026, 6, 6), date(2026, 6, 6))

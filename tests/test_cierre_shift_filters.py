"""Unit tests for cierre shift-window filter builders (issue #685)."""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.services.cierre_service import (
    _compute_preview,
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


def test_open_tables_filter_covers_rebel_rebel_night_opened_at():
    opened_bogota = datetime(2026, 6, 6, 21, 52, tzinfo=BOG)
    opened_utc = opened_bogota.astimezone(timezone.utc)

    assert opened_utc.date() == date(2026, 6, 7)

    sql, params = _build_open_tables_filter(
        date(2026, 6, 6), date(2026, 6, 6), None, None
    )

    assert "(ts.opened_at AT TIME ZONE 'America/Bogota')::date" in sql
    assert "opened_at::date" not in sql
    assert params == [date(2026, 6, 6), date(2026, 6, 6)]


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
    assert conn.open_tables_args == ("tenant-id", date(2026, 6, 6), date(2026, 6, 6))

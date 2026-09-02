"""Cierre preview/breakdown includes cartera abonos and wallet recharges (#957)."""
from datetime import date, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from app.services.cierre_service import _compute_breakdown_rows, _compute_preview
from app.services.credit_service import (
    fetch_credit_payment_breakdown_for_cierre,
    fetch_credit_payment_totals_for_cierre,
)

BOG = ZoneInfo("America/Bogota")
TENANT_ID = uuid4()


class _CreditPaymentConn:
    def __init__(self, rows):
        self.rows = rows
        self.last_sql = None
        self.last_args = None

    async def fetch(self, sql, *args):
        self.last_sql = sql
        self.last_args = args
        return self.rows


@pytest.mark.asyncio
async def test_fetch_credit_payment_totals_date_only_uses_payment_date():
    conn = _CreditPaymentConn([{"group_slug": "cash", "method_name": "Efectivo", "total": 50_000.0}])
    totals = await fetch_credit_payment_totals_for_cierre(
        conn,
        TENANT_ID,
        date(2026, 5, 18),
        date(2026, 5, 18),
    )
    assert totals == {"cash": 50_000.0}
    assert "payment_date::date >=" in conn.last_sql
    assert conn.last_args[1:] == (date(2026, 5, 18), date(2026, 5, 18))


@pytest.mark.asyncio
async def test_fetch_credit_payment_totals_shift_uses_half_open_window():
    start = datetime(2026, 5, 18, 14, 0, tzinfo=BOG)
    end = datetime(2026, 5, 18, 22, 0, tzinfo=BOG)
    conn = _CreditPaymentConn([{"group_slug": "card", "method_name": "Tarjeta", "total": 30_000.0}])
    totals = await fetch_credit_payment_totals_for_cierre(
        conn,
        TENANT_ID,
        date(2026, 5, 18),
        date(2026, 5, 18),
        start,
        end,
    )
    assert totals == {"card": 30_000.0}
    assert "payment_date >= $2" in conn.last_sql
    assert "payment_date < $3" in conn.last_sql
    assert conn.last_args[1:] == (start, end)


@pytest.mark.asyncio
async def test_fetch_credit_payment_breakdown_returns_method_rows():
    conn = _CreditPaymentConn(
        [
            {"group_slug": "cash", "method_name": "Efectivo", "total": 20_000.0},
            {"group_slug": "card", "method_name": "Datafono", "total": 10_000.0},
        ]
    )
    rows = await fetch_credit_payment_breakdown_for_cierre(
        conn,
        TENANT_ID,
        date(2026, 5, 18),
        date(2026, 5, 18),
    )
    assert rows == [
        {"group_slug": "cash", "method_name": "Efectivo", "total": 20_000.0},
        {"group_slug": "card", "method_name": "Datafono", "total": 10_000.0},
    ]


class _PreviewConn:
    async def fetchrow(self, sql, *args):
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
        if "open_tables_count" in sql:
            return {"open_tables_count": 0}
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def fetch(self, *_args):
        return []


@pytest.mark.asyncio
async def test_preview_adds_credit_abonos_to_cash_expected(monkeypatch):
    async def credit_cash(*_args, **_kwargs):
        return {"cash": 25_000.0}

    async def no_wallet(*_args, **_kwargs):
        return {}

    async def no_advances(*_args, **_kwargs):
        return {"collections": {}, "applications": {}, "cover": {"total": 0.0}}

    monkeypatch.setattr(
        "app.services.credit_service.fetch_credit_payment_totals_for_cierre",
        credit_cash,
    )
    monkeypatch.setattr(
        "app.services.customer_wallet_service.fetch_wallet_recharge_totals_for_cierre",
        no_wallet,
    )
    monkeypatch.setattr(
        "app.services.table_session_advances_service.fetch_table_session_advance_totals_for_cierre",
        no_advances,
    )

    preview = await _compute_preview(
        _PreviewConn(),
        tenant_id=TENANT_ID,
        period_start=date(2026, 5, 18),
        period_end=date(2026, 5, 18),
        opening_cash=100_000.0,
    )

    assert preview["totalCash"] == 25_000.0
    assert preview["cashExpected"] == 125_000.0


class _BreakdownConn:
    async def fetch(self, sql, *args):
        if "FROM order_payments" in sql or "FROM orders o" in sql:
            return []
        raise AssertionError(f"unexpected fetch SQL: {sql}")


@pytest.mark.asyncio
async def test_breakdown_adds_wallet_and_cartera_inflow_rows(monkeypatch):
    async def wallet_cash(*_args, **_kwargs):
        return {"cash": 15_000.0}

    async def credit_rows(*_args, **_kwargs):
        return [{"group_slug": "cash", "method_name": "Efectivo", "total": 25_000.0}]

    async def no_advances(*_args, **_kwargs):
        return {"collections": {}, "applications": {}, "cover": {"total": 0.0}}

    monkeypatch.setattr(
        "app.services.customer_wallet_service.fetch_wallet_recharge_totals_for_cierre",
        wallet_cash,
    )
    monkeypatch.setattr(
        "app.services.credit_service.fetch_credit_payment_breakdown_for_cierre",
        credit_rows,
    )
    monkeypatch.setattr(
        "app.services.table_session_advances_service.fetch_table_session_advance_totals_for_cierre",
        no_advances,
    )

    rows = await _compute_breakdown_rows(
        _BreakdownConn(),
        tenant_id=TENANT_ID,
        period_start=date(2026, 5, 18),
        period_end=date(2026, 5, 18),
    )

    by_name = {row["method_name"]: row["total"] for row in rows}
    assert by_name["Recarga billetera - cash"] == 15_000.0
    assert by_name["Abono cartera - Efectivo"] == 25_000.0

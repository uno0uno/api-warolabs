"""Gastos a crédito + Pagos settlement (#2113 / epic #2109)."""

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services.account_role_service import AccountRole, MissingAccountRoleError
from app.services.expenses_service import (
    CONTADO_REQUIRES_PAYMENT_METHOD,
    _normalize_payment_type,
    _post_expense_gl_entry,
    _post_expense_payment_gl_entry,
    assert_contado_requires_payment_method,
)


def test_normalize_payment_type_defaults_and_rejects_unknown():
    assert _normalize_payment_type(None) == "contado"
    assert _normalize_payment_type("CREDITO") == "credito"
    with pytest.raises(HTTPException) as exc:
        _normalize_payment_type("partial")
    assert exc.value.status_code == 400


def test_contado_requires_payment_method():
    assert_contado_requires_payment_method("credito", None)
    with pytest.raises(HTTPException) as exc:
        assert_contado_requires_payment_method("contado", None)
    assert CONTADO_REQUIRES_PAYMENT_METHOD in exc.value.detail


@pytest.mark.asyncio
async def test_credit_create_gl_credits_accounts_payable_role():
    tenant_id = uuid4()
    expense_id = uuid4()
    debit_id = uuid4()
    ap_id = uuid4()
    entry_id = uuid4()

    conn = MagicMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "debit_account_code": "5105",
                "credit_cash_account_code": "1105",
                "credit_default_account_code": "1110",
            },
            {"id": debit_id, "code": "5105"},
            {"id": entry_id},
        ]
    )
    conn.fetchval = AsyncMock(return_value=None)
    line_args: list[tuple] = []

    async def _execute(sql, *args):
        if "INSERT INTO tenant_journal_lines" in sql:
            line_args.append(args)
        return "INSERT 0 1"

    conn.execute = AsyncMock(side_effect=_execute)
    resolve_account = AsyncMock(return_value=SimpleNamespace(id=ap_id, code="2205"))

    with patch("app.services.expenses_service.resolve_account", resolve_account):
        await _post_expense_gl_entry(
            conn,
            tenant_id,
            expense_id,
            amount=80.0,
            transaction_date=date(2026, 8, 4),
            description="Arriendo agosto",
            category_code="RENT",
            payment_method=None,
            payment_type="credito",
        )

    resolve_account.assert_awaited_once()
    assert resolve_account.await_args.args[2] == AccountRole.ACCOUNTS_PAYABLE
    assert line_args[0][1] == debit_id
    assert line_args[1][1] == ap_id


@pytest.mark.asyncio
async def test_contado_create_gl_soft_skips_without_mapping():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)

    await _post_expense_gl_entry(
        conn,
        uuid4(),
        uuid4(),
        amount=10.0,
        transaction_date=date(2026, 8, 4),
        description="Contado",
        category_code="MISC",
        payment_method="cash",
        payment_type="contado",
    )
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_expense_payment_gl_debits_ap():
    tenant_id = uuid4()
    expense_id = uuid4()
    ap_id = uuid4()
    cash_id = uuid4()
    entry_id = uuid4()

    conn = MagicMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    conn.fetchval = AsyncMock(side_effect=[None, None])  # no existing; period open
    conn.fetchrow = AsyncMock(return_value={"id": entry_id})
    line_args: list[tuple] = []

    async def _execute(sql, *args):
        if "INSERT INTO tenant_journal_lines" in sql:
            line_args.append(args)
        return "INSERT 0 1"

    conn.execute = AsyncMock(side_effect=_execute)

    with patch(
        "app.services.expenses_service.resolve_tenant_timezone",
        AsyncMock(return_value="America/Bogota"),
    ), patch(
        "app.services.expenses_service.resolve_account",
        AsyncMock(return_value=SimpleNamespace(id=ap_id, code="2000")),
    ) as resolve_account, patch(
        "app.services.expenses_service.resolve_payment_account",
        AsyncMock(return_value=SimpleNamespace(id=cash_id, code="1000")),
    ):
        await _post_expense_payment_gl_entry(
            conn=conn,
            tenant_id=tenant_id,
            expense_id=expense_id,
            amount=80.0,
            payment_date=datetime(2026, 8, 4, tzinfo=timezone.utc),
            description="Pago gasto WR-G-1",
            payment_method="cash",
            payment_method_id=None,
        )

    assert resolve_account.await_args.args[2] == AccountRole.ACCOUNTS_PAYABLE
    assert line_args[0][1] == ap_id
    assert line_args[1][1] == cash_id


def test_missing_ap_role_is_explicit_conflict():
    err = MissingAccountRoleError(uuid4(), AccountRole.ACCOUNTS_PAYABLE, source="expense_credit")
    assert err.status_code == 409
    assert err.details["code"] == "ACCOUNT_ROLE_MISSING"


def test_migration_121_is_add_only():
    sql = open("migrations/121_expense_credit_payables.sql").read()
    assert "ADD COLUMN IF NOT EXISTS payment_type" in sql
    assert "ADD COLUMN IF NOT EXISTS paid_at" in sql
    assert "DROP COLUMN" not in sql
    assert "DROP TABLE" not in sql


def test_expense_payments_quota_registered():
    from app.services import billing_service

    assert "expense_payments_per_period" in billing_service.PERIOD_QUOTA_RESOURCES
    assert "expense_payments_per_period" in billing_service.QUOTA_KEYS
    assert billing_service.STARTER_OPERATIONAL_QUOTAS["expense_payments_per_period"] == 30


@pytest.mark.asyncio
async def test_update_expense_json_blocks_paid_credit():
    from contextlib import asynccontextmanager
    from app.models.expense import ExpenseUpdate
    from app.services.expenses_service import update_expense_json

    tenant_id = uuid4()
    user_id = uuid4()
    expense_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "id": expense_id,
            "tenant_id": tenant_id,
            "payment_type": "credito",
            "paid_at": datetime.now(timezone.utc),
        }
    )
    conn.execute = MagicMock()

    @asynccontextmanager
    async def _db_ctx():
        yield conn

    with patch(
        "app.services.expenses_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id, user_id=user_id),
    ), patch(
        "app.services.expenses_service.get_db_connection",
        side_effect=_db_ctx,
    ):
        with pytest.raises(HTTPException) as exc:
            await update_expense_json(
                MagicMock(),
                MagicMock(),
                expense_id,
                ExpenseUpdate(description="nope"),
            )

    assert exc.value.status_code == 409
    assert "ya pagado" in exc.value.detail
    conn.execute.assert_not_called()

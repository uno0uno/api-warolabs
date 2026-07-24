from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.core.exceptions import APIError
from app.services import credit_service
from app.services.accounting_service import _compute_pl_for_period
from app.services.account_role_service import (
    AccountRef,
    AccountRole,
    MissingAccountRoleError,
    ensure_colombia_payroll,
    ensure_matias_dian,
    resolve_account,
    resolve_payment_account,
)


@pytest.mark.asyncio
async def test_resolver_prefers_tenant_override_and_is_code_independent():
    tenant_id = uuid4()
    account_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={
        "id": account_id,
        "code": "LOCAL-BANK-RENAMED",
        "name": "Operating account renamed",
        "binding_source": "tenant_override",
    })

    account = await resolve_account(conn, tenant_id, AccountRole.BANK, source="test")

    assert account == AccountRef(
        account_id,
        "LOCAL-BANK-RENAMED",
        "Operating account renamed",
        AccountRole.BANK,
        "tenant_override",
    )
    query = conn.fetchrow.await_args.args[0]
    assert "binding.role = $2" in query
    assert "accounts.code" not in query


@pytest.mark.asyncio
async def test_missing_required_role_is_explicit():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)

    with pytest.raises(MissingAccountRoleError) as exc:
        await resolve_account(conn, tenant_id, AccountRole.COGS, source="order_cogs")

    assert exc.value.role == AccountRole.COGS
    assert exc.value.source == "order_cogs"


@pytest.mark.asyncio
async def test_payment_account_prefers_explicit_uuid_binding():
    tenant_id = uuid4()
    method_id = uuid4()
    account = AccountRef(uuid4(), "BANK-01", "Bank", None, "explicit_account_id")
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={
        "method_account_id": account.id,
        "method_legacy_code": "legacy",
        "group_account_id": None,
        "group_legacy_code": None,
        "group_tenant_id": None,
        "slug": "digital",
    })

    with patch(
        "app.services.account_role_service.resolve_account_by_id",
        new=AsyncMock(return_value=account),
    ) as by_id, patch(
        "app.services.account_role_service.resolve_legacy_account",
        new=AsyncMock(),
    ) as legacy:
        resolved = await resolve_payment_account(
            conn, tenant_id, "digital", payment_method_id=method_id
        )

    assert resolved == account
    by_id.assert_awaited_once_with(conn, tenant_id, account.id)
    legacy.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_role_aborts_credit_journal_before_header_insert():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock()

    with patch(
        "app.services.credit_service._resolve_credit_payment_debit_account",
        new=AsyncMock(side_effect=MissingAccountRoleError(tenant_id, AccountRole.BANK)),
    ):
        with pytest.raises(MissingAccountRoleError):
            await credit_service._post_credit_payment_gl(
                conn,
                tenant_id,
                uuid4(),
                uuid4(),
                Decimal("10"),
                "digital",
                uuid4(),
                None,
                date(2026, 7, 14),
                uuid4(),
            )

    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_colombia_payroll_gate_rejects_global_profile():
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=False)

    with pytest.raises(APIError) as exc:
        await ensure_colombia_payroll(conn, uuid4())

    assert exc.value.status_code == 409
    assert exc.value.details["code"] == "COLOMBIA_PAYROLL_NOT_AVAILABLE"


@pytest.mark.asyncio
async def test_matias_dian_gate_rejects_global_profile():
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=False)

    with pytest.raises(APIError) as exc:
        await ensure_matias_dian(conn, uuid4())

    assert exc.value.status_code == 409
    assert exc.value.details["code"] == "MATIAS_DIAN_NOT_AVAILABLE"


@pytest.mark.asyncio
async def test_matias_dian_gate_allows_colombia_profile():
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=True)

    await ensure_matias_dian(conn, uuid4())
    conn.fetchval.assert_awaited_once()


@pytest.mark.asyncio
async def test_global_pl_does_not_query_or_include_colombia_payroll():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[
        {"total_sales": 0},
        {"total": 0},
        {"total": 0},
    ])
    conn.fetch = AsyncMock(return_value=[])

    result = await _compute_pl_for_period(
        conn, uuid4(), 2026, 7, include_colombia_payroll=False
    )

    assert result.operating_expenses.payroll == 0
    assert result.provisions.total == 0
    assert conn.fetchrow.await_count == 3
    queries = "\n".join(call.args[0] for call in conn.fetchrow.await_args_list)
    assert "salary_payments" not in queries
    assert "employee_salaries" not in queries


def test_migration_defines_scoped_uuid_bindings_and_compatibility_backfill():
    migration = Path("migrations/105_account_role_bindings.sql").read_text()

    assert "CREATE TABLE IF NOT EXISTS accounting_roles" in migration
    assert "CREATE TABLE IF NOT EXISTS tenant_account_role_overrides" in migration
    assert "FOREIGN KEY (tenant_id, tenant_account_id)" in migration
    assert "ADD COLUMN IF NOT EXISTS gl_account_id UUID" in migration
    assert "methods.gl_account_id IS NULL" in migration
    assert "config.inc_gl_account_id IS NULL" in migration

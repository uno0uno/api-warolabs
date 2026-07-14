from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.services.account_role_service import (
    AccountRef,
    AccountRole,
    MissingAccountRoleError,
)
from app.services.salary_service import (
    _post_provision_gl_entries,
    _post_salary_gl_entry,
    _resolve_salary_credit_account,
)


@pytest.mark.asyncio
async def test_salary_uuid_payment_uses_semantic_payment_resolver():
    tenant_id = uuid4()
    method_id = uuid4()
    expected = AccountRef(uuid4(), "BANK", "Bank", AccountRole.BANK, "explicit_account_id")
    conn = MagicMock()

    with patch(
        "app.services.salary_service.resolve_payment_account",
        new=AsyncMock(return_value=expected),
    ) as resolver:
        account = await _resolve_salary_credit_account(conn, tenant_id, str(method_id))

    assert account == expected
    resolver.assert_awaited_once_with(
        conn,
        tenant_id,
        "transfer",
        payment_method_id=method_id,
        source="salary",
    )


@pytest.mark.asyncio
async def test_salary_missing_expense_role_does_not_insert_journal_header():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchval = AsyncMock(side_effect=[None, None])
    conn.fetchrow = AsyncMock()

    with patch(
        "app.services.salary_service.resolve_account",
        new=AsyncMock(
            side_effect=MissingAccountRoleError(
                tenant_id, AccountRole.PAYROLL_EXPENSE, "salary"
            )
        ),
    ):
        with pytest.raises(MissingAccountRoleError):
            await _post_salary_gl_entry(
                conn,
                tenant_id,
                uuid4(),
                date(2026, 7, 14),
                Decimal("100"),
                "employee",
                "cash",
                "Salary",
            )

    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_provision_missing_role_resolves_before_mutation_or_header():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchval = AsyncMock(side_effect=[None, 0, None])
    conn.fetchrow = AsyncMock()
    conn.execute = AsyncMock()

    with patch(
        "app.services.salary_service.resolve_account",
        new=AsyncMock(
            side_effect=MissingAccountRoleError(
                tenant_id, AccountRole.PRIMA_EXPENSE, "salary_provision"
            )
        ),
    ):
        with pytest.raises(MissingAccountRoleError):
            await _post_provision_gl_entries(
                conn,
                tenant_id,
                uuid4(),
                "2026-07",
                "employee",
                Decimal("1200"),
            )

    conn.execute.assert_not_awaited()
    conn.fetchrow.assert_not_awaited()

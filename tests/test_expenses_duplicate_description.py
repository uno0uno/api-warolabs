"""Duplicate expense descriptions are allowed (api-warolabs#602)."""

from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import asyncpg
import pytest
from fastapi import HTTPException

from app.core.middleware import SessionContext
from app.models.expense import ExpenseCreate, ExpenseUpdate
from app.services.expenses_service import (
    _raise_duplicate_expense_error,
    create_expense_json,
    update_expense_json,
)


DUPLICATE_DESCRIPTION_CONSTRAINT = (
    "tenant_expenses_tenant_id_expense_category_id_month_year_de_key"
)


def _session(tenant_id, user_id):
    return SessionContext({
        "user_id": user_id,
        "tenant_id": tenant_id,
        "email": "test@warocol.com",
        "name": "Test",
        "expires_at": None,
        "is_active": True,
    })


def _expense_row(tenant_id, category_id, description="Servicio domicilio"):
    now = datetime(2026, 7, 9, 12, 0, 0)
    return {
        "id": uuid4(),
        "tenant_id": tenant_id,
        "expense_category_id": category_id,
        "month_year": "2026-07",
        "amount": 12000,
        "description": description,
        "source_system": "manual",
        "expense_number": "WR-GTO-2026-0001",
        "created_at": now,
        "transaction_date": date(2026, 7, 9),
        "is_recurring": False,
        "frequency": None,
        "recurring_end_date": None,
        "payment_method": "cash",
        "payment_method_id": None,
        "expense_type": "admin_expense",
        "cat_id": category_id,
        "category_code": "ADM",
        "category_name": "Administrativos",
        "cat_description": None,
        "cat_active": True,
    }


def test_duplicate_description_constraint_is_not_mapped_to_409():
    exc = asyncpg.UniqueViolationError("duplicate key")
    exc.constraint_name = DUPLICATE_DESCRIPTION_CONSTRAINT

    with pytest.raises(asyncpg.UniqueViolationError):
        _raise_duplicate_expense_error(exc)


def test_legacy_duplicate_description_message_removed():
    exc = asyncpg.UniqueViolationError("duplicate key")
    exc.constraint_name = DUPLICATE_DESCRIPTION_CONSTRAINT

    try:
        _raise_duplicate_expense_error(exc)
    except HTTPException as http_exc:
        assert http_exc.status_code != 409
        assert "misma categoría, mes y descripción" not in str(http_exc.detail)
    except asyncpg.UniqueViolationError:
        pass


def test_migration_drops_duplicate_description_uniqueness():
    migration = Path("migrations/098_allow_duplicate_expense_descriptions.sql").read_text()

    assert "DROP CONSTRAINT IF EXISTS" in migration
    assert "DROP INDEX IF EXISTS" in migration
    assert DUPLICATE_DESCRIPTION_CONSTRAINT in migration


@pytest.mark.asyncio
async def test_create_expense_json_allows_repeated_description_flow():
    tenant_id = uuid4()
    user_id = uuid4()
    category_id = uuid4()
    full_expense = _expense_row(tenant_id, category_id)

    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=True)
    conn.fetchrow = AsyncMock(side_effect=[
        {"id": full_expense["id"], "created_at": full_expense["created_at"]},
        full_expense,
    ])

    @asynccontextmanager
    async def _db_ctx():
        yield conn

    body = ExpenseCreate(
        expenseCategoryId=category_id,
        amount=12000,
        description="Servicio domicilio",
        transactionDate=date(2026, 7, 9),
        expenseType="admin_expense",
    )

    with patch(
        "app.services.expenses_service.require_valid_session",
        return_value=_session(tenant_id, user_id),
    ), patch(
        "app.services.expenses_service.get_db_connection",
        side_effect=_db_ctx,
    ), patch(
        "app.services.expenses_service.get_next_expense_number",
        AsyncMock(return_value="WR-GTO-2026-0001"),
    ), patch(
        "app.services.expenses_service._post_expense_gl_entry",
        AsyncMock(),
    ):
        result = await create_expense_json(MagicMock(), MagicMock(), body)

    insert_sql = conn.fetchrow.await_args_list[0].args[0]
    assert "INSERT INTO tenant_expenses" in insert_sql
    assert "ON CONFLICT" not in insert_sql
    assert result.data.description == "Servicio domicilio"


@pytest.mark.asyncio
async def test_update_expense_json_allows_repeated_description_flow():
    tenant_id = uuid4()
    user_id = uuid4()
    category_id = uuid4()
    expense_id = uuid4()
    full_expense = _expense_row(tenant_id, category_id)
    full_expense["id"] = expense_id

    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[
        {"id": expense_id, "tenant_id": tenant_id},
        full_expense,
    ])
    conn.execute = AsyncMock()
    conn.transaction.return_value.__aenter__ = AsyncMock()
    conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)

    @asynccontextmanager
    async def _db_ctx():
        yield conn

    body = ExpenseUpdate(description="Servicio domicilio")

    with patch(
        "app.services.expenses_service.require_valid_session",
        return_value=_session(tenant_id, user_id),
    ), patch(
        "app.services.expenses_service.get_db_connection",
        side_effect=_db_ctx,
    ), patch(
        "app.services.expenses_service._void_expense_gl_entry",
        AsyncMock(),
    ), patch(
        "app.services.expenses_service._post_expense_gl_entry",
        AsyncMock(),
    ):
        result = await update_expense_json(MagicMock(), MagicMock(), expense_id, body)

    update_sql = conn.execute.await_args.args[0]
    assert "UPDATE tenant_expenses" in update_sql
    assert "description =" in update_sql
    assert result.data.description == "Servicio domicilio"

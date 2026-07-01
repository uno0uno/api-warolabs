"""Idempotency tests for expense retry paths (api-warolabs#566)."""
from contextlib import asynccontextmanager
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.core.middleware import SessionContext
from app.models.expense import RecurringExpenseInstanceCreate
from app.services.expenses_service import create_recurring_instance_json


def _session(tenant_id, user_id):
    return SessionContext({
        "user_id": user_id,
        "tenant_id": tenant_id,
        "email": "test@warocol.com",
        "name": "Test",
        "expires_at": None,
        "is_active": True,
    })


@pytest.mark.asyncio
async def test_create_recurring_instance_retry_returns_existing_period():
    tenant_id = uuid4()
    user_id = uuid4()
    expense_id = uuid4()
    instance_id = uuid4()
    now = datetime(2026, 7, 1, 12, 0, 0)
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=instance_id)
    conn.fetchrow = AsyncMock(side_effect=[
        {"id": expense_id, "is_recurring": True, "amount": 50000},
        {
            "id": instance_id,
            "tenant_id": tenant_id,
            "expense_id": expense_id,
            "period_month": "2026-07",
            "scheduled_date": date(2026, 7, 5),
            "amount": 50000,
            "status": "pending",
            "payment_date": None,
            "payment_method": None,
            "payment_reference": None,
            "notes": None,
            "created_by": user_id,
            "created_at": now,
            "updated_at": now,
        },
    ])

    @asynccontextmanager
    async def _db_ctx():
        yield conn

    body = RecurringExpenseInstanceCreate(
        periodMonth="2026-07",
        scheduledDate=date(2026, 7, 5),
        status="pending",
    )

    with patch(
        "app.services.expenses_service.require_valid_session",
        return_value=_session(tenant_id, user_id),
    ), patch(
        "app.services.expenses_service.get_db_connection",
        side_effect=_db_ctx,
    ):
        result = await create_recurring_instance_json(
            MagicMock(),
            MagicMock(),
            expense_id,
            body,
        )

    insert_sql = conn.fetchval.await_args.args[0]
    assert "ON CONFLICT (expense_id, period_month)" in insert_sql
    assert result["success"] is True
    assert result["data"]["id"] == str(instance_id)
    assert result["data"]["periodMonth"] == "2026-07"

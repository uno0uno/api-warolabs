"""Billing exempt tenant IDs — issue #1057."""
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from app.services import billing_service


WAROCOLOMBIA_ID = UUID("93b3e582-34fa-44a6-8d0f-bf82a3608727")
OTHER_ID = UUID("11111111-1111-1111-1111-111111111111")


@pytest.mark.asyncio
async def test_exempt_tenant_returns_full_access(monkeypatch):
    monkeypatch.setattr(
        billing_service.settings,
        "billing_exempt_tenant_ids",
        str(WAROCOLOMBIA_ID),
    )
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"status": "past_due", "current_period_end": None, "plan_id": None})

    access = await billing_service.get_subscription_access(WAROCOLOMBIA_ID, conn)

    assert access.level == "full"
    conn.fetchrow.assert_not_called()


@pytest.mark.asyncio
async def test_non_exempt_tenant_still_queries_subscription(monkeypatch):
    monkeypatch.setattr(billing_service.settings, "billing_exempt_tenant_ids", "")
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)

    access = await billing_service.get_subscription_access(OTHER_ID, conn)

    assert access.level == "free"
    conn.fetchrow.assert_called_once()

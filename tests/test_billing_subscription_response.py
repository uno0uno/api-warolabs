"""Tenant subscription response fields used by the Mi Plan recovery UI."""
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.services import billing_service


def _subscription_row(*, status: str, checkout_url: Optional[str]):
    now = datetime.now(timezone.utc)
    return {
        "id": uuid4(),
        "tenant_id": uuid4(),
        "tenant_name": "Test tenant",
        "plan_id": uuid4(),
        "plan_name": "Pro",
        "plan_slug": "pro",
        "scan_limit": 1000,
        "plan_features": {},
        "billing_cycle": "annual",
        "status": status,
        "current_period_start": now,
        "current_period_end": now,
        "gateway_reference": "wompi-link",
        "cancelled_at": None,
        "created_at": now,
        "updated_at": now,
        "checkout_url": checkout_url,
        "scans_used": 0,
    }


@pytest.mark.asyncio
async def test_pending_subscription_exposes_latest_checkout_url():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value=_subscription_row(
            status="pending",
            checkout_url="https://checkout.wompi.co/l/pending-link",
        )
    )

    result = await billing_service.get_tenant_subscription(conn, uuid4())

    assert result["checkout_url"] == "https://checkout.wompi.co/l/pending-link"
    query = conn.fetchrow.await_args.args[0]
    assert "FROM billing_events be" in query
    assert "be.subscription_id = ts.id" in query
    assert "be.event_type = 'subscribe_initiated'" in query
    assert "WHEN ts.status = 'pending'" in query


@pytest.mark.asyncio
async def test_active_subscription_does_not_expose_checkout_url():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value=_subscription_row(status="active", checkout_url=None)
    )

    result = await billing_service.get_tenant_subscription(conn, uuid4())

    assert result["checkout_url"] is None

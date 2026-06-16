from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.services import notifications_service


@pytest.mark.asyncio
async def test_terms_acceptance_reminder_for_active_tenant_pending_terms():
    tenant_id = uuid4()
    conn = MagicMock()

    with patch(
        "app.services.notifications_service.billing_service.get_subscription_access",
        new=AsyncMock(return_value=SimpleNamespace(subscription_status="active")),
    ), patch(
        "app.services.notifications_service.legal_service.get_terms_status",
        new=AsyncMock(return_value={
            "success": True,
            "data": {
                "requires_acceptance": True,
                "current": {"version_id": str(uuid4()), "version": "1.0"},
                "acceptance": None,
            },
        }),
    ):
        notification = await notifications_service._build_terms_acceptance_notification(conn, tenant_id)

    assert notification is not None
    assert notification["type"] == "terms_acceptance_required"
    assert notification["order_id"] is None
    assert notification["payload"]["version"] == "1.0"
    assert notification["payload"]["return_to"] == "/gestion/billing"


@pytest.mark.asyncio
async def test_terms_acceptance_reminder_for_pending_checkout_subscription():
    tenant_id = uuid4()
    conn = MagicMock()

    with patch(
        "app.services.notifications_service.billing_service.get_subscription_access",
        new=AsyncMock(return_value=SimpleNamespace(subscription_status="pending")),
    ), patch(
        "app.services.notifications_service.legal_service.get_terms_status",
        new=AsyncMock(return_value={
            "success": True,
            "data": {
                "requires_acceptance": True,
                "current": {"version_id": str(uuid4()), "version": "1.0"},
                "acceptance": None,
            },
        }),
    ):
        notification = await notifications_service._build_terms_acceptance_notification(conn, tenant_id)

    assert notification is not None
    assert notification["type"] == "terms_acceptance_required"
    assert notification["payload"]["return_to"] == "/gestion/billing"


@pytest.mark.asyncio
async def test_terms_acceptance_reminder_skips_accepted_terms():
    tenant_id = uuid4()
    conn = MagicMock()

    with patch(
        "app.services.notifications_service.billing_service.get_subscription_access",
        new=AsyncMock(return_value=SimpleNamespace(subscription_status="active")),
    ), patch(
        "app.services.notifications_service.legal_service.get_terms_status",
        new=AsyncMock(return_value={
            "success": True,
            "data": {
                "requires_acceptance": False,
                "current": {"version_id": str(uuid4()), "version": "1.0"},
                "acceptance": {"id": str(uuid4())},
            },
        }),
    ):
        notification = await notifications_service._build_terms_acceptance_notification(conn, tenant_id)

    assert notification is None

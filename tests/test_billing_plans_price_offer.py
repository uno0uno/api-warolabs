"""GET /billing/plans includes regional price_offer (#796)."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import permissions
from app.core.middleware import SessionContext
from app.routers.billing import tenant_router


@pytest.fixture(autouse=True)
def _clear_caches():
    permissions._enforcement_mode_cache.clear()
    permissions._role_modules_cache.clear()
    yield
    permissions._enforcement_mode_cache.clear()
    permissions._role_modules_cache.clear()


def test_tenant_list_plans_includes_price_offer_for_co():
    tenant_id = uuid4()
    session = SessionContext({
        "user_id": uuid4(),
        "tenant_id": tenant_id,
        "email": "test@example.com",
        "name": "Test",
        "expires_at": None,
        "is_active": True,
        "role": "owner",
    })
    plan = {
        "id": str(uuid4()),
        "name": "Pro",
        "slug": "pro",
        "is_active": True,
        "price_monthly": "0",
        "price_annual": "95900",
    }

    @asynccontextmanager
    async def _db(**_kwargs):
        conn = MagicMock()
        conn.fetchval = AsyncMock(return_value="disabled")
        conn.fetchrow = AsyncMock(return_value=None)
        conn.fetch = AsyncMock(return_value=[])
        yield conn

    app = FastAPI()
    app.include_router(tenant_router)

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.routers.billing.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_db), \
         patch("app.routers.billing.get_db_connection", side_effect=_db), \
         patch("app.routers.billing.billing_service.list_plans", new=AsyncMock(return_value=[plan])), \
         patch(
             "app.routers.billing.billing_service.get_tenant_billing_context",
             new=AsyncMock(return_value={"slug": "demo", "country_code": "CO"}),
         ):
        client = TestClient(app)
        res = client.get("/billing/plans")

    assert res.status_code == 200
    body = res.json()
    assert "plans" in body
    assert body["plans"][0]["slug"] == "pro"
    assert body["price_offer"]["currency"] == "USD"
    assert body["price_offer"]["segment"] == "usd_9"
    assert body["price_offer"]["annual_amount_minor"] == 9000
    assert body["price_offer"]["annual_amount"] == 90.0

"""POST /billing/subscribe — annual-only for new subscriptions (#877)."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core import permissions
from app.core.middleware import SessionContext
from app.routers import billing
from app.routers.billing import tenant_router


@pytest.fixture(autouse=True)
def _clear_caches():
    permissions._enforcement_mode_cache.clear()
    permissions._role_modules_cache.clear()
    yield
    permissions._enforcement_mode_cache.clear()
    permissions._role_modules_cache.clear()


def _build_app():
    tenant_id = uuid4()
    user_id = uuid4()
    session = SessionContext({
        "user_id": user_id,
        "tenant_id": tenant_id,
        "email": "test@example.com",
        "name": "Test User",
        "expires_at": None,
        "is_active": True,
        "role": "owner",
    })
    app = FastAPI()
    app.include_router(tenant_router)
    return app, session


def test_subscribe_rejects_monthly_billing_cycle():
    app, session = _build_app()
    plan_id = uuid4()

    @asynccontextmanager
    async def _enforce_db():
        conn = MagicMock()
        conn.fetchval = AsyncMock(return_value="enforce")
        conn.fetchrow = AsyncMock(return_value=None)
        conn.fetch = AsyncMock(return_value=[])
        yield conn

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.routers.billing.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db):
        client = TestClient(app)
        res = client.post(
            "/billing/subscribe",
            json={"plan_id": str(plan_id), "billing_cycle": "monthly"},
        )

    assert res.status_code == 422
    assert "anual" in res.json()["detail"].lower()


def test_subscribe_requires_current_terms_before_paddle_checkout():
    app, session = _build_app()
    plan_id = uuid4()

    @asynccontextmanager
    async def _enforce_db():
        conn = MagicMock()
        conn.fetchval = AsyncMock(return_value="disabled")
        conn.fetchrow = AsyncMock(return_value=None)
        conn.fetch = AsyncMock(return_value=[])
        yield conn

    conn = MagicMock()

    @asynccontextmanager
    async def _billing_db():
        yield conn

    terms_error = HTTPException(
        status_code=409,
        detail={
            "code": "terms_acceptance_required",
            "document_version_id": str(uuid4()),
            "version": "1.0",
        },
    )

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.routers.billing.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db), \
         patch("app.routers.billing.get_db_connection", side_effect=_billing_db), \
         patch("app.routers.billing.billing_service.get_plan_for_subscribe", new=AsyncMock(return_value={
             "id": str(plan_id),
             "name": "Plan Pro",
             "price_annual": 1200000.0,
         })), \
         patch("app.routers.billing.legal_service.ensure_current_terms_accepted", new=AsyncMock(side_effect=terms_error)), \
         patch("app.routers.billing.paddle_service.create_checkout", new=AsyncMock()) as paddle_mock:
        client = TestClient(app)
        res = client.post(
            "/billing/subscribe",
            json={"plan_id": str(plan_id), "billing_cycle": "annual"},
        )

    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "terms_acceptance_required"
    paddle_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_active_promoted_tenant_uses_normal_subscription_flow():
    tenant_id = uuid4()
    plan_id = uuid4()
    session = SessionContext({
        "user_id": uuid4(),
        "tenant_id": tenant_id,
        "email": "test@example.com",
        "name": "Test User",
        "expires_at": None,
        "is_active": True,
        "role": "admin",
        "lifecycle_status": "active",
        "onboarding_state": "payment_pending",
    })

    conn = MagicMock()

    @asynccontextmanager
    async def _billing_db():
        yield conn

    plan = {
        "id": plan_id,
        "name": "Plan Pro",
        "price_annual": 1200000,
        "amount_in_cents": 120000000,
    }
    checkout = {
        "checkout_url": "https://checkout.example.test",
        "gateway_reference": "txn_paddle_normal",
    }
    subscribed = {"status": "pending", "checkout_url": checkout["checkout_url"]}

    with patch.object(billing, "require_valid_session", return_value=session), patch.object(
        billing, "get_db_connection", side_effect=_billing_db
    ), patch.object(
        billing.billing_service,
        "get_plan_for_subscribe",
        new=AsyncMock(return_value=plan),
    ), patch.object(
        billing.legal_service,
        "ensure_current_terms_accepted",
        new=AsyncMock(),
    ) as terms, patch.object(
        billing.billing_service,
        "ensure_subscribe_allowed",
        new=AsyncMock(),
    ), patch.object(
        billing.billing_service,
        "get_tenant_billing_context",
        new=AsyncMock(return_value={"slug": "demo", "country_code": "CO"}),
    ), patch.object(
        billing.paddle_service,
        "create_checkout",
        new=AsyncMock(return_value=checkout),
    ), patch.object(
        billing.billing_service,
        "subscribe_tenant",
        new=AsyncMock(return_value=subscribed),
    ) as subscribe, patch.object(
        billing.billing_service,
        "create_onboarding_payment_attempt",
        new=AsyncMock(),
    ) as onboarding_attempt:
        result = await billing.subscribe(
            billing.SubscribeBody(plan_id=plan_id, billing_cycle="annual"),
            MagicMock(),
        )

    assert result == subscribed
    terms.assert_awaited_once_with(conn, tenant_id)
    subscribe.assert_awaited_once()
    onboarding_attempt.assert_not_awaited()

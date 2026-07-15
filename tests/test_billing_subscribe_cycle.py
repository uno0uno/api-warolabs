"""POST /billing/subscribe — annual-only for new subscriptions (#877)."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
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


def test_subscribe_requires_current_terms_before_wompi_link():
    app, session = _build_app()
    plan_id = uuid4()

    @asynccontextmanager
    async def _enforce_db():
        conn = MagicMock()
        conn.fetchval = AsyncMock(return_value="disabled")
        conn.fetchrow = AsyncMock(return_value=None)
        conn.fetch = AsyncMock(return_value=[])
        yield conn

    @asynccontextmanager
    async def _billing_db():
        yield MagicMock()

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
         patch("app.routers.billing.billing_service.tenant_has_trial_subscription", new=AsyncMock(return_value=False)), \
         patch("app.routers.billing.billing_service.get_plan_for_subscribe", new=AsyncMock(return_value={
             "id": str(plan_id),
             "name": "Plan Pro",
             "price_annual": 1200000.0,
         })), \
         patch("app.routers.billing.legal_service.ensure_current_terms_accepted", new=AsyncMock(side_effect=terms_error)), \
         patch("app.routers.billing.wompi_service.create_payment_link", new=AsyncMock()) as wompi_mock:
        client = TestClient(app)
        res = client.post(
            "/billing/subscribe",
            json={"plan_id": str(plan_id), "billing_cycle": "annual"},
        )

    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "terms_acceptance_required"
    wompi_mock.assert_not_awaited()

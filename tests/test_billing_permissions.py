"""End-to-end smoke tests for billing endpoints under require_module(MI_PLAN).

Validates that the wiring in `app/routers/billing.py` actually runs the
dependency before reaching the handler. Pairs with the unit tests in
`test_permissions_dependency.py` (which validate the dependency itself in
isolation).

Two scenarios:
  * Owner role under enforce → 200 (or whatever the handler returns).
  * Cashier role under enforce → 403 raised by require_module.

The webhook + cron endpoints are intentionally NOT tested here — they don't
go through the dependency.
"""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core import permissions
from app.core.middleware import SessionContext
from app.core.permissions import Module
from app.routers.billing import tenant_router


# ─── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_caches():
    permissions._enforcement_mode_cache.clear()
    permissions._role_modules_cache.clear()
    yield
    permissions._enforcement_mode_cache.clear()
    permissions._role_modules_cache.clear()


def _build_app(role):
    """Build a minimal FastAPI app mounting only the billing tenant_router.

    The auth flow is short-circuited by:
      * `require_valid_session` patched to return a fresh SessionContext.
      * `get_session_context` patched to return that same SessionContext.
    """
    tenant_id = uuid4()
    user_id = uuid4()
    session = SessionContext({
        "user_id": user_id,
        "tenant_id": tenant_id,
        "email": "test@example.com",
        "name": "Test User",
        "expires_at": None,
        "is_active": True,
        "role": role,
    })

    app = FastAPI()
    app.include_router(tenant_router)
    return app, session


@asynccontextmanager
async def _fake_db_ctx_returning(value):
    """Async context manager whose .fetchval returns `value` on demand."""
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=value)
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    yield conn


# ─── Tests ────────────────────────────────────────────────────────────


def test_owner_role_passes_billing_under_enforce():
    """Owner reaches the handler under enforce mode — dependency permits."""
    app, session = _build_app(role="owner")

    # Stub the service call so we don't touch the DB schema.
    fake_access = MagicMock(
        level="full",
        grace_days_remaining=None,
        subscription_status="active",
        next_payment_date=None,
        message=None,
    )

    @asynccontextmanager
    async def _enforce_db():
        conn = MagicMock()
        # get_enforcement_mode reads the first conn it gets — return 'enforce'.
        conn.fetchval = AsyncMock(return_value="enforce")
        conn.fetchrow = AsyncMock(return_value=None)
        conn.fetch = AsyncMock(return_value=[])
        yield conn

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.routers.billing.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db), \
         patch(
             "app.routers.billing.billing_service.get_subscription_access",
             new=AsyncMock(return_value=fake_access),
         ), \
         patch(
             "app.routers.billing.get_db_connection",
             side_effect=lambda use_transaction=True: _fake_db_ctx_returning("enforce"),
         ):
        client = TestClient(app)
        response = client.get("/billing/access-status")

    # Handler ran (dependency permitted owner). Body shape matters less than
    # the fact that we did NOT get 403.
    assert response.status_code == 200
    assert response.json()["level"] == "full"


def test_cashier_role_denied_billing_under_enforce():
    """Cashier hits 403 before the handler runs — dependency rejects."""
    app, session = _build_app(role="cashier")

    @asynccontextmanager
    async def _enforce_db():
        conn = MagicMock()
        conn.fetchval = AsyncMock(return_value="enforce")
        conn.fetch = AsyncMock(return_value=[])  # no overrides → defaults apply
        yield conn

    # Stub get_role_modules so the dependency sees cashier's default set
    # (POS, VENTAS only — no MI_PLAN) without touching the DB twice.
    cashier_modules = frozenset({Module.POS, Module.VENTAS})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.routers.billing.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=cashier_modules),
         ):
        client = TestClient(app)
        response = client.get("/billing/access-status")

    assert response.status_code == 403
    assert "mi_plan" in response.json()["detail"].lower()

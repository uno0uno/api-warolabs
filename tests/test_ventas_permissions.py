"""End-to-end smoke tests for VENTAS group endpoints under require_module(VENTAS).

Sub-task E2.4 of Epic 2 (#189). Validates that:
1. Owner role under enforce reaches the handler.
2. Cashier/kitchen roles under enforce get 403 (they lack VENTAS in default
   matrix — same matrix gap surfaced by E2.3 / POS).

No KDS-passthrough test needed: VENTAS routers (customers, online_orders,
orders) have NO endpoint exclusions — all 28 endpoints are gated.

Pairs with `tests/test_pos_permissions.py` (#188 reference impl).
"""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import permissions
from app.core.middleware import SessionContext
from app.core.permissions import Module
from app.routers.orders import router as orders_router


# ─── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_caches():
    permissions._enforcement_mode_cache.clear()
    permissions._role_modules_cache.clear()
    yield
    permissions._enforcement_mode_cache.clear()
    permissions._role_modules_cache.clear()


def _build_session(role):
    return SessionContext({
        "user_id": uuid4(),
        "tenant_id": uuid4(),
        "email": "test@example.com",
        "name": "Test User",
        "expires_at": None,
        "is_active": True,
        "role": role,
    })


def _enforce_db_ctx():
    @asynccontextmanager
    async def _ctx():
        conn = MagicMock()
        conn.fetchval = AsyncMock(return_value="enforce")
        conn.fetch = AsyncMock(return_value=[])
        yield conn
    return _ctx


# ─── Tests ────────────────────────────────────────────────────────────


def test_owner_role_passes_ventas_endpoint_under_enforce():
    """Owner reaches GET /orders/dashboard under enforce — dependency permits."""
    session = _build_session(role="owner")
    app = FastAPI()
    app.include_router(orders_router)

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.routers.orders.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.routers.orders.orders_service.get_orders_dashboard",
             new=AsyncMock(return_value={"data": []}),
         ):
        client = TestClient(app)
        response = client.get("/orders/dashboard")

    # Handler reached → dependency permitted owner.
    assert response.status_code == 200


def test_kitchen_role_denied_ventas_endpoint_under_enforce():
    """Kitchen role hits 403 on VENTAS — kitchen lacks VENTAS in default matrix."""
    session = _build_session(role="kitchen")
    app = FastAPI()
    app.include_router(orders_router)

    # Stub get_role_modules to return kitchen's default set (DESPACHO only).
    kitchen_modules = frozenset({Module.DESPACHO})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.routers.orders.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=kitchen_modules),
         ):
        client = TestClient(app)
        response = client.get("/orders/dashboard")

    assert response.status_code == 403
    assert "ventas" in response.json()["detail"].lower()


def test_cashier_role_denied_ventas_endpoint_under_enforce():
    """Cashier role hits 403 on VENTAS — cashier defaults are POS-only."""
    session = _build_session(role="cashier")
    app = FastAPI()
    app.include_router(orders_router)

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.routers.orders.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.routers.orders.orders_service.get_orders_dashboard",
             new=AsyncMock(return_value={"data": []}),
         ) as dashboard:
        client = TestClient(app)
        response = client.get("/orders/dashboard")

    assert response.status_code == 403
    assert "ventas" in response.json()["detail"].lower()
    dashboard.assert_not_awaited()

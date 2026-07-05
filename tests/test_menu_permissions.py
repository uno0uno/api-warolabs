"""End-to-end smoke tests for MENU group endpoints under require_module(MENU).

Sub-task E2.6 of Epic 2 (#190). Validates that:
1. Cashier role under enforce gets 403 on MENU because POS catalog reads now
   go through /pos/products instead of admin Menu endpoints.
2. Kitchen role under enforce gets 403 on a MENU endpoint — kitchen lacks
   MENU (only has DESPACHO), so the gate denies as expected.

Pairs with `tests/test_ventas_permissions.py` (#189 reference impl).
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
from app.routers.products import router as products_router


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


def test_cashier_role_denied_menu_endpoint_under_enforce():
    """Cashier hits 403 on MENU — cashier defaults are POS-only."""
    session = _build_session(role="cashier")
    app = FastAPI()
    app.include_router(products_router, prefix="/menu/products")

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.routers.products.get_products_list",
             new=AsyncMock(return_value={"data": [], "total": 0}),
         ) as get_products:
        client = TestClient(app)
        response = client.get("/menu/products")

    assert response.status_code == 403
    assert "menu" in response.json()["detail"].lower()
    get_products.assert_not_awaited()


def test_kitchen_role_denied_menu_endpoint_under_enforce():
    """Kitchen role hits 403 on MENU — kitchen lacks MENU in default matrix."""
    session = _build_session(role="kitchen")
    app = FastAPI()
    app.include_router(products_router, prefix="/menu/products")

    # Stub get_role_modules to return kitchen's default set (DESPACHO only).
    kitchen_modules = frozenset({Module.DESPACHO})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=kitchen_modules),
         ):
        client = TestClient(app)
        response = client.get("/menu/products")

    assert response.status_code == 403
    assert "menu" in response.json()["detail"].lower()

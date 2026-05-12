"""End-to-end smoke tests for ABASTECIMIENTO group endpoints under require_module(ABASTECIMIENTO).

Sub-task E2.8 of Epic 2 (#195). Validates that:
1. Owner role under enforce reaches the suppliers/ingredients handler.
2. Cashier role under enforce gets 403 (cashier lacks ABASTECIMIENTO —
   admin + supervisor only in the default matrix).

Scope covers 6 files / 62 endpoints (admin_ingredients, ingredient_purchase_units,
ingredients, inventory, purchases, suppliers). The gate is applied uniformly to
every endpoint; smoke tests pick the highest-volume one to validate the pattern.

Pairs with `tests/test_facturacion_permissions.py` (#194 reference impl).
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
from app.routers.ingredients import router as ingredients_router


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


def test_owner_role_passes_abastecimiento_endpoint_under_enforce():
    """Owner reaches GET /suppliers/ingredients under enforce — dependency permits."""
    session = _build_session(role="owner")
    app = FastAPI()
    app.include_router(ingredients_router, prefix="/suppliers/ingredients")

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.routers.ingredients.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.routers.ingredients.get_ingredients_list",
             new=AsyncMock(return_value={"data": [], "total": 0}),
         ):
        client = TestClient(app)
        response = client.get("/suppliers/ingredients")

    # Handler reached → dependency permitted owner.
    assert response.status_code == 200


def test_cashier_role_denied_abastecimiento_endpoint_under_enforce():
    """Cashier role hits 403 on ABASTECIMIENTO — cashier lacks ABASTECIMIENTO in default matrix."""
    session = _build_session(role="cashier")
    app = FastAPI()
    app.include_router(ingredients_router, prefix="/suppliers/ingredients")

    # Stub get_role_modules to return cashier's default set (POS + VENTAS only).
    cashier_modules = frozenset({Module.POS, Module.VENTAS})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.routers.ingredients.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=cashier_modules),
         ):
        client = TestClient(app)
        response = client.get("/suppliers/ingredients")

    assert response.status_code == 403
    assert "abastecimiento" in response.json()["detail"].lower()

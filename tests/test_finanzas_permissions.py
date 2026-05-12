"""End-to-end smoke tests for FINANZAS group endpoints under require_module(FINANZAS).

Sub-task E2.10 of Epic 2 (#198). Validates that:
1. Owner role under enforce reaches the handler.
2. Cashier role under enforce gets 403 (cashier lacks FINANZAS in default matrix).
3. Regression guard: payment_methods.pos_router still works under POS module
   for cashier role (proves the bulk regex did not accidentally rewrite the POS
   gate at app/routers/payment_methods.py:68).

Uses `accounting.py` as the representative FINANZAS router (largest after salaries).
Pattern follows `tests/test_ventas_permissions.py` (#189 reference impl).
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
from app.routers.accounting import router as accounting_router
from app.routers.payment_methods import pos_router as payment_methods_pos_router


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


def test_owner_role_passes_finanzas_endpoint_under_enforce():
    """Owner reaches GET /accounting/accounts under enforce — dependency permits."""
    session = _build_session(role="owner")
    app = FastAPI()
    app.include_router(accounting_router, prefix="/accounting")

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.routers.accounting.get_accounts",
             new=AsyncMock(return_value={"success": True, "data": []}),
         ):
        client = TestClient(app)
        response = client.get("/accounting/accounts")

    # Handler reached → dependency permitted owner.
    assert response.status_code == 200


def test_cashier_role_denied_finanzas_endpoint_under_enforce():
    """Cashier role hits 403 on FINANZAS — cashier holds only POS+VENTAS in matrix."""
    session = _build_session(role="cashier")
    app = FastAPI()
    app.include_router(accounting_router, prefix="/accounting")

    cashier_modules = frozenset({Module.POS, Module.VENTAS})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=cashier_modules),
         ):
        client = TestClient(app)
        response = client.get("/accounting/accounts")

    assert response.status_code == 403
    assert "finanzas" in response.json()["detail"].lower()


def test_cashier_role_passes_pos_router_under_enforce_regression():
    """Regression guard: cashier still passes payment_methods.pos_router (POS module).

    The bulk regex in this PR targeted `@finanzas_router.*` only. This test confirms
    `@pos_router.get("", dependencies=[Depends(require_module(Module.POS))])` at
    app/routers/payment_methods.py:68 still carries Module.POS — i.e. the regex
    did not accidentally rewrite it.
    """
    session = _build_session(role="cashier")
    app = FastAPI()
    app.include_router(payment_methods_pos_router)

    cashier_modules = frozenset({Module.POS, Module.VENTAS})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=cashier_modules),
         ), \
         patch(
             "app.routers.payment_methods.payment_method_service.list_pos_methods",
             new=AsyncMock(return_value={"success": True, "data": []}),
         ):
        client = TestClient(app)
        response = client.get("/pos/payment-methods")

    # If the regex had rewritten this decorator to FINANZAS, cashier would 403.
    assert response.status_code == 200

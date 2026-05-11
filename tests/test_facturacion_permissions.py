"""End-to-end smoke tests for FACTURACION group endpoints under require_module(FACTURACION).

Sub-task E2.11 of Epic 2 (#194). Validates that:
1. Owner role under enforce reaches the documents handler.
2. Cashier role under enforce gets 403 (cashier lacks FACTURACION —
   FACTURACION is admin-only in the default matrix).

Scope covers 4 files / 15 endpoints (documents.py, facturacion.py with its
3 sub-routers, invoices.py, support_documents.py). 12 of 15 are stub 503s
waiting for api-facturacion upstream (issue #129); 3 endpoints in
documents.py are live. The gate is applied identically to all 15.

POS checkout invoice emit is unaffected — it routes through
`/api/orders/{id}/invoice` (gated under VENTAS in #189), NOT through
any endpoint in this PR's scope.

Pairs with `tests/test_analitica_permissions.py` (#193 reference impl).
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
from app.routers.documents import router as documents_router


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


def test_owner_role_passes_facturacion_endpoint_under_enforce():
    """Owner reaches GET /api/documents under enforce — dependency permits."""
    session = _build_session(role="owner")
    app = FastAPI()
    app.include_router(documents_router)

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.routers.documents.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.routers.documents.facturacion_service.get_documents_list",
             new=AsyncMock(return_value={"data": [], "total": 0}),
         ):
        client = TestClient(app)
        response = client.get("/api/documents")

    # Handler reached → dependency permitted owner.
    assert response.status_code == 200


def test_cashier_role_denied_facturacion_endpoint_under_enforce():
    """Cashier role hits 403 on FACTURACION — cashier lacks FACTURACION in default matrix."""
    session = _build_session(role="cashier")
    app = FastAPI()
    app.include_router(documents_router)

    # Stub get_role_modules to return cashier's default set (POS + VENTAS only).
    cashier_modules = frozenset({Module.POS, Module.VENTAS})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.routers.documents.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=cashier_modules),
         ):
        client = TestClient(app)
        response = client.get("/api/documents")

    assert response.status_code == 403
    assert "facturacion" in response.json()["detail"].lower()

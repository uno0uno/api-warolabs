"""Permission smoke tests for legal terms endpoints."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import permissions
from app.core.middleware import SessionContext
from app.core.permissions import Module
from app.routers.legal import router as legal_router


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


def _legal_db_ctx():
    @asynccontextmanager
    async def _ctx(**_kwargs):
        yield MagicMock()
    return _ctx


def test_owner_role_passes_legal_audit_under_enforce():
    """Owner reaches legal audit because MI_NEGOCIO is owner-only."""
    session = _build_session(role="owner")
    app = FastAPI()
    app.include_router(legal_router)

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.routers.legal.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch("app.routers.legal.get_db_connection", side_effect=_legal_db_ctx()), \
         patch(
             "app.routers.legal.legal_service.list_acceptance_audit_records",
             new=AsyncMock(return_value={"success": True, "data": {"records": []}}),
         ):
        client = TestClient(app)
        response = client.get("/legal/terms/audit")

    assert response.status_code == 200


def test_cashier_role_denied_legal_audit_under_enforce():
    """Cashier hits 403 on legal audit because it exposes tenant compliance history."""
    session = _build_session(role="cashier")
    app = FastAPI()
    app.include_router(legal_router)

    cashier_modules = frozenset({Module.POS, Module.VENTAS})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=cashier_modules),
         ):
        client = TestClient(app)
        response = client.get("/legal/terms/audit")

    assert response.status_code == 403
    assert "mi_negocio" in response.json()["detail"].lower()


def test_cashier_role_can_still_read_terms_status_under_enforce():
    """Terms status remains a global-auth exception so acceptance is never blocked."""
    session = _build_session(role="cashier")
    app = FastAPI()
    app.include_router(legal_router)

    with patch("app.routers.legal.require_valid_session", return_value=session), \
         patch("app.routers.legal.get_db_connection", side_effect=_legal_db_ctx()), \
         patch(
             "app.routers.legal.legal_service.get_terms_status",
             new=AsyncMock(return_value={"success": True, "data": {"accepted": False}}),
         ):
        client = TestClient(app)
        response = client.get("/legal/terms/status")

    assert response.status_code == 200


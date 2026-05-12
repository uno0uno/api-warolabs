"""Tests for GET /me/access — surfaces effective role + modules + enforcement_mode.

Sub-task E2.17 of Epic 2 (#200). Validates that:
1. Owner role under enforce reaches the handler and gets ALL modules.
2. Cashier role returns only ["pos", "ventas"] (sorted).
3. Session with role=None short-circuits to empty modules without crashing.
4. Tenant with enforcement_mode='disabled' is reported correctly.
5. Tenant with enforcement_mode='enforce' is reported correctly.
6. Unauthenticated request returns 401 (require_valid_session raises).

The endpoint itself is intentionally ungated by Module — it IS the source of
truth for those gates and cannot gate itself. Tests confirm that no Module
dependency stands between the caller and the response.
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
from app.routers.me import router as me_router


# ─── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_caches():
    permissions._enforcement_mode_cache.clear()
    permissions._role_modules_cache.clear()
    yield
    permissions._enforcement_mode_cache.clear()
    permissions._role_modules_cache.clear()


def _build_session(role, tenant_id=None):
    return SessionContext({
        "user_id": uuid4(),
        "tenant_id": tenant_id or uuid4(),
        "email": "test@example.com",
        "name": "Test User",
        "expires_at": None,
        "is_active": True,
        "role": role,
    })


def _mock_db_ctx(enforcement_mode="disabled"):
    @asynccontextmanager
    async def _ctx():
        conn = MagicMock()
        conn.fetchval = AsyncMock(return_value=enforcement_mode)
        conn.fetch = AsyncMock(return_value=[])
        yield conn
    return _ctx


# ─── Tests ────────────────────────────────────────────────────────────


def test_owner_returns_all_modules():
    """Owner sees the full module set, sorted alphabetically."""
    session = _build_session(role="owner")
    app = FastAPI()
    app.include_router(me_router, prefix="/me")

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.routers.me.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_mock_db_ctx("disabled")):
        client = TestClient(app)
        response = client.get("/me/access")

    assert response.status_code == 200
    body = response.json()
    assert body["role"] == "owner"
    # Owner gets every module in the enum, sorted.
    expected = sorted(m.value for m in Module)
    assert body["modules"] == expected
    assert body["enforcement_mode"] == "disabled"


def test_cashier_returns_pos_ventas_only():
    """Cashier sees only [pos, ventas] per DEFAULT_ROLE_MODULES."""
    session = _build_session(role="cashier")
    app = FastAPI()
    app.include_router(me_router, prefix="/me")

    cashier_modules = frozenset({Module.POS, Module.VENTAS})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.routers.me.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_mock_db_ctx("disabled")), \
         patch(
             "app.routers.me.get_role_modules",
             new=AsyncMock(return_value=cashier_modules),
         ):
        client = TestClient(app)
        response = client.get("/me/access")

    assert response.status_code == 200
    body = response.json()
    assert body["role"] == "cashier"
    assert body["modules"] == ["pos", "ventas"]


def test_null_role_returns_empty_modules():
    """Session with role=None (KDS-token, fresh tenant) returns empty modules without crash."""
    session = _build_session(role=None)
    app = FastAPI()
    app.include_router(me_router, prefix="/me")

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.routers.me.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_mock_db_ctx("shadow")):
        client = TestClient(app)
        response = client.get("/me/access")

    assert response.status_code == 200
    body = response.json()
    assert body["role"] is None
    assert body["modules"] == []
    # enforcement_mode still reported — tenant_id was present, only role was None.
    assert body["enforcement_mode"] == "shadow"


def test_enforcement_mode_disabled_reported():
    """Tenant in 'disabled' mode reports it in the response."""
    session = _build_session(role="admin")
    app = FastAPI()
    app.include_router(me_router, prefix="/me")

    admin_modules = frozenset({Module.POS, Module.VENTAS, Module.FINANZAS})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.routers.me.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_mock_db_ctx("disabled")), \
         patch(
             "app.routers.me.get_role_modules",
             new=AsyncMock(return_value=admin_modules),
         ):
        client = TestClient(app)
        response = client.get("/me/access")

    assert response.status_code == 200
    assert response.json()["enforcement_mode"] == "disabled"


def test_enforcement_mode_enforce_reported():
    """Tenant in 'enforce' mode reports it in the response."""
    session = _build_session(role="admin")
    app = FastAPI()
    app.include_router(me_router, prefix="/me")

    admin_modules = frozenset({Module.POS, Module.VENTAS, Module.FINANZAS})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.routers.me.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_mock_db_ctx("enforce")), \
         patch(
             "app.routers.me.get_role_modules",
             new=AsyncMock(return_value=admin_modules),
         ):
        client = TestClient(app)
        response = client.get("/me/access")

    assert response.status_code == 200
    assert response.json()["enforcement_mode"] == "enforce"


def test_unauthenticated_returns_401():
    """No valid session → require_valid_session raises → 401."""
    from app.core.exceptions import api_exception_handler, APIError

    app = FastAPI()
    app.add_exception_handler(APIError, api_exception_handler)
    app.include_router(me_router, prefix="/me")

    # Empty SessionContext is is_valid=False
    empty_session = SessionContext()

    with patch("app.core.middleware.get_session_context", return_value=empty_session):
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/me/access")

    assert response.status_code == 401

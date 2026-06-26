"""End-to-end smoke tests for EQUIPO group endpoints under require_module(EQUIPO).

Sub-task E2.12 of Epic 2 (#196). Validates that:
1. Owner role under enforce reaches a gated tenants/members endpoint.
2. Cashier role under enforce gets 403 on the gated endpoint (EQUIPO is
   owner-only by matrix; cashier/admin/supervisor/kitchen all lack it).
3. **Exclusion regression test**: cashier successfully reaches
   GET /tenants/user-tenants under enforce — proves the sidebar tenant
   switcher exclusion works for non-owner roles.

Scope: 2 files, 8 total endpoints (6 gated, 2 excluded):
  - tenants.py: 3 gated (/members CRUD), 1 excluded (/user-tenants);
    POST "" tenant self-service creation was removed in #546.
  - invitations.py: 3 gated (/send, /pending, DELETE), 1 excluded (/accept)

The 3rd test is critical regression protection — if anyone later
accidentally gates /user-tenants under EQUIPO, the sidebar tenant
switcher would 403 for every non-owner role across all WARO tenants.

Pairs with `tests/test_abastecimiento_permissions.py` (#195 reference impl).
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
from app.routers.tenants import router as tenants_router


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


def test_owner_role_passes_equipo_endpoint_under_enforce():
    """Owner reaches GET /tenants/members under enforce — dependency permits."""
    session = _build_session(role="owner")
    app = FastAPI()
    app.include_router(tenants_router, prefix="/tenants")

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.routers.tenants.get_tenant_members",
             new=AsyncMock(return_value={"data": [], "total": 0}),
         ):
        client = TestClient(app)
        response = client.get("/tenants/members")

    # Handler reached → dependency permitted owner.
    assert response.status_code == 200


def test_cashier_role_denied_equipo_endpoint_under_enforce():
    """Cashier role hits 403 on EQUIPO — cashier lacks EQUIPO in default matrix."""
    session = _build_session(role="cashier")
    app = FastAPI()
    app.include_router(tenants_router, prefix="/tenants")

    # Stub get_role_modules to return cashier's default set (POS + VENTAS only).
    cashier_modules = frozenset({Module.POS, Module.VENTAS})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=cashier_modules),
         ):
        client = TestClient(app)
        response = client.get("/tenants/members")

    assert response.status_code == 403
    assert "equipo" in response.json()["detail"].lower()


def test_cashier_passes_user_tenants_exclusion_under_enforce():
    """Cashier reaches GET /tenants/user-tenants under enforce — EXCLUSION test.

    This endpoint is NOT gated under EQUIPO because the sidebar tenant
    switcher (stores/tenants.ts:59) fires it for every authenticated user.
    Gating it would break the switcher for cashier/kitchen/admin/supervisor.

    This test catches accidental regression — if anyone adds the dependency
    later, this test fails and the sidebar protection is enforced at the
    test layer.
    """
    session = _build_session(role="cashier")
    app = FastAPI()
    app.include_router(tenants_router, prefix="/tenants")

    cashier_modules = frozenset({Module.POS, Module.VENTAS})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=cashier_modules),
         ), \
         patch(
             "app.routers.tenants.get_user_tenants",
             new=AsyncMock(return_value={"success": True, "data": []}),
         ):
        client = TestClient(app)
        response = client.get("/tenants/user-tenants")

    # NO 403 — handler reached, cashier passed because endpoint is ungated.
    # If this asserts != 200, someone gated /user-tenants by mistake and the
    # sidebar tenant switcher would break for every non-owner role.
    assert response.status_code == 200


def test_post_tenants_self_service_create_is_closed_under_enforce():
    """POST /tenants is no longer a self-service tenant provisioning surface."""
    session = _build_session(role="cashier")
    app = FastAPI()
    app.include_router(tenants_router, prefix="/tenants")

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session):
        client = TestClient(app)
        response = client.post("/tenants", json={"name": "Should Not Provision"})

    assert response.status_code in (404, 405)

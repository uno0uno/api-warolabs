"""End-to-end smoke tests for MI_NEGOCIO group endpoints (tenant_config.py).

Sub-task E2.15 of Epic 2 (#192). MI_NEGOCIO is owner-only by business rule —
ADMIN and SUPERVISOR were stripped of MI_NEGOCIO in the matrix as part of
the E2.7/E2.15 PR.

Validates that:
1. Owner reaches a tenant-config endpoint under enforce.
2. Cashier hits 403 on a tenant-config endpoint (proves owner-only).
3. Admin hits 403 too (proves matrix tightening — admin used to have it).

POS pages no longer call /api/tenant/*; they consume /api/pos/restaurant-context
instead (tested in test_pos_permissions.py).
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
from app.routers.tenant_config import router as tenant_config_router


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


def test_owner_role_passes_mi_negocio_endpoint_under_enforce():
    """Owner reaches GET /api/tenant/public-profile under enforce."""
    session = _build_session(role="owner")
    app = FastAPI()
    app.include_router(tenant_config_router, prefix="/api/tenant")

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.routers.tenant_config.tenant_config_service.get_own_public_profile",
             new=AsyncMock(return_value=None),
         ):
        client = TestClient(app)
        response = client.get("/api/tenant/public-profile")

    assert response.status_code == 200


def test_cashier_role_denied_mi_negocio_under_enforce():
    """Cashier hits 403 on MI_NEGOCIO — owner-only by business rule."""
    session = _build_session(role="cashier")
    app = FastAPI()
    app.include_router(tenant_config_router, prefix="/api/tenant")

    cashier_modules = frozenset({Module.POS, Module.VENTAS})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=cashier_modules),
         ):
        client = TestClient(app)
        response = client.get("/api/tenant/public-profile")

    assert response.status_code == 403
    assert "mi_negocio" in response.json()["detail"].lower()


def test_admin_role_denied_mi_negocio_under_enforce():
    """Admin hits 403 on MI_NEGOCIO — matrix tightened, owner-only."""
    session = _build_session(role="admin")
    app = FastAPI()
    app.include_router(tenant_config_router, prefix="/api/tenant")

    # Admin matrix after E2.7/E2.15: no MI_NEGOCIO (only kept POS..MI_PLAN sans EQUIPO/MI_NEGOCIO).
    admin_modules = frozenset({
        Module.POS, Module.VENTAS, Module.DESPACHO, Module.MENU,
        Module.OPERACIONES, Module.ABASTECIMIENTO, Module.ANALITICA,
        Module.FINANZAS, Module.FACTURACION, Module.INTEGRACIONES,
        Module.MI_PLAN,
    })

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=admin_modules),
         ):
        client = TestClient(app)
        response = client.get("/api/tenant/public-profile")

    assert response.status_code == 403
    assert "mi_negocio" in response.json()["detail"].lower()

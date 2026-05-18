"""Permission tests for /cierre vs /operaciones/shifts split (warocol.com#689).

Arqueo execution (including shift-template read for wizards) requires FINANZAS.
Template CRUD requires OPERACIONES. Default matrix: owner/admin have finanzas;
supervisor/cashier do not unless tenant override.
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
from app.routers.cierre import router as cierre_router
from app.routers.operaciones_shifts import router as operaciones_shifts_router

ADMIN_MODULES = frozenset({
    Module.POS, Module.VENTAS, Module.DESPACHO, Module.MENU,
    Module.OPERACIONES, Module.ABASTECIMIENTO, Module.ANALITICA,
    Module.FINANZAS, Module.FACTURACION, Module.INTEGRACIONES, Module.MI_PLAN,
})
SUPERVISOR_MODULES = frozenset({
    Module.POS, Module.VENTAS, Module.DESPACHO, Module.MENU,
    Module.OPERACIONES, Module.ABASTECIMIENTO, Module.ANALITICA,
})
CASHIER_MODULES = frozenset({Module.POS, Module.VENTAS, Module.MENU})


@pytest.fixture(autouse=True)
def _clear_caches():
    permissions._enforcement_mode_cache.clear()
    permissions._role_modules_cache.clear()
    yield
    permissions._enforcement_mode_cache.clear()
    permissions._role_modules_cache.clear()


def _build_session(role: str):
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


def test_admin_passes_cierre_and_shifts_under_enforce():
    session = _build_session(role="admin")
    app = FastAPI()
    app.include_router(cierre_router)
    app.include_router(operaciones_shifts_router)

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.services.cierre_service.require_valid_session", return_value=session), \
         patch("app.services.shift_templates_service.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch("app.core.permissions.get_role_modules", new=AsyncMock(return_value=ADMIN_MODULES)), \
         patch("app.services.cierre_service.get_ultimo_cierre", new=AsyncMock(return_value={"success": True, "data": None})), \
         patch("app.services.shift_templates_service.list_shift_templates", new=AsyncMock(return_value={"success": True, "data": []})):
        client = TestClient(app)
        assert client.get("/cierre/ultimo").status_code == 200
        assert client.get("/cierre/shift-templates").status_code == 200
        assert client.get("/operaciones/shifts").status_code == 200


def test_supervisor_denied_cierre_allowed_shifts_under_enforce():
    session = _build_session(role="supervisor")
    app = FastAPI()
    app.include_router(cierre_router)
    app.include_router(operaciones_shifts_router)

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.services.shift_templates_service.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch("app.core.permissions.get_role_modules", new=AsyncMock(return_value=SUPERVISOR_MODULES)), \
         patch("app.services.shift_templates_service.list_shift_templates", new=AsyncMock(return_value={"success": True, "data": []})):
        client = TestClient(app)
        denied = client.get("/cierre/ultimo")
        assert denied.status_code == 403
        assert "finanzas" in denied.json()["detail"].lower()
        assert client.get("/operaciones/shifts").status_code == 200


def test_cashier_denied_cierre_and_shifts_under_enforce():
    session = _build_session(role="cashier")
    app = FastAPI()
    app.include_router(cierre_router)
    app.include_router(operaciones_shifts_router)

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.core.middleware.require_valid_session", return_value=session), \
         patch("app.services.shift_templates_service.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch("app.core.permissions.get_role_modules", new=AsyncMock(return_value=CASHIER_MODULES)):
        client = TestClient(app)
        cierre_resp = client.get("/cierre/ultimo")
        assert cierre_resp.status_code == 403
        assert "finanzas" in cierre_resp.json()["detail"].lower()
        shifts_resp = client.get("/operaciones/shifts")
        assert shifts_resp.status_code == 403
        assert "operaciones" in shifts_resp.json()["detail"].lower()

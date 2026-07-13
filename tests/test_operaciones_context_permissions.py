"""End-to-end smoke tests for the operaciones-context router (#210).

Validates the BFF aggregator + 5 toggle PATCH endpoints introduced as the
enforce-prep follow-up to #191/#192 (#209). MI_NEGOCIO stays owner-only;
supervisor/admin read and toggle operational features via OPERACIONES.

Coverage:
1. Supervisor reaches GET /api/operaciones/restaurant-context (read).
2. Kitchen denied on the GET (proves the gate).
3. Cashier denied on a PATCH toggle (proves OPERACIONES, not POS, is the gate).
4. Supervisor passes a PATCH toggle and the service receives correct args.
5. update_toggle rejects an unknown column name with 422 (whitelist guard).

Pairs with `tests/test_pos_permissions.py` (#209 reference impl).
"""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core import permissions
from app.core.middleware import SessionContext
from app.core.permissions import Module
from app.routers.operaciones_context import router as operaciones_context_router
from app.services.operaciones_context_service import update_toggle


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


def test_supervisor_passes_operaciones_context_under_enforce():
    """Supervisor reaches GET /operaciones/restaurant-context under enforce."""
    session = _build_session(role="supervisor")
    app = FastAPI()
    app.include_router(operaciones_context_router)

    supervisor_modules = frozenset({
        Module.POS, Module.VENTAS, Module.DESPACHO, Module.MENU,
        Module.OPERACIONES, Module.ABASTECIMIENTO, Module.ANALITICA,
    })

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.routers.operaciones_context.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=supervisor_modules),
         ), \
         patch(
             "app.routers.operaciones_context.get_operaciones_context",
             new=AsyncMock(return_value={
                 "display_name": "Demo",
                 "kds_enabled": True,
                 "comandas_enabled": True,
                 "expediter_enabled": False,
                 "tables_enabled": True,
                 "accepts_online_orders": False,
                 "auto_select_generic_enabled": False,
                 "fiscal_data": {},
                 "tax_config": {},
                 "invoicing_ready": False,
             }),
         ):
        client = TestClient(app)
        response = client.get("/operaciones/restaurant-context")

    assert response.status_code == 200
    assert response.json()["data"]["display_name"] == "Demo"


def test_kitchen_denied_operaciones_context_under_enforce():
    """Kitchen hits 403 on operaciones GET — kitchen lacks OPERACIONES."""
    session = _build_session(role="kitchen")
    app = FastAPI()
    app.include_router(operaciones_context_router)

    kitchen_modules = frozenset({Module.DESPACHO})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.routers.operaciones_context.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=kitchen_modules),
         ):
        client = TestClient(app)
        response = client.get("/operaciones/restaurant-context")

    assert response.status_code == 403
    assert "operaciones" in response.json()["detail"].lower()


def test_cashier_denied_operaciones_toggle_under_enforce():
    """Cashier hits 403 on PATCH /operaciones/toggles/kds — POS != OPERACIONES."""
    session = _build_session(role="cashier")
    app = FastAPI()
    app.include_router(operaciones_context_router)

    cashier_modules = frozenset({Module.POS, Module.VENTAS, Module.MENU})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.routers.operaciones_context.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=cashier_modules),
         ):
        client = TestClient(app)
        response = client.patch("/operaciones/toggles/kds", json={"enabled": True})

    assert response.status_code == 403
    assert "operaciones" in response.json()["detail"].lower()


def test_supervisor_passes_toggle_kds_under_enforce():
    """Supervisor toggles kds_enabled; service receives the right column + value."""
    session = _build_session(role="supervisor")
    app = FastAPI()
    app.include_router(operaciones_context_router)

    supervisor_modules = frozenset({
        Module.POS, Module.VENTAS, Module.OPERACIONES,
    })
    toggle_stub = AsyncMock(return_value={"success": True, "data": {"kds_enabled": True}})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.routers.operaciones_context.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=supervisor_modules),
         ), \
         patch(
             "app.routers.operaciones_context.update_toggle",
             new=toggle_stub,
         ):
        client = TestClient(app)
        response = client.patch("/operaciones/toggles/kds", json={"enabled": True})

    assert response.status_code == 200
    assert response.json()["data"]["kds_enabled"] is True
    # Service was invoked with (tenant_id, "kds_enabled", True).
    args, _ = toggle_stub.call_args
    assert args[1] == "kds_enabled"
    assert args[2] is True


def test_supervisor_passes_toggle_open_sale_under_enforce():
    """Supervisor toggles venta libre; set_open_sale_enabled receives enabled flag (#805)."""
    session = _build_session(role="supervisor")
    app = FastAPI()
    app.include_router(operaciones_context_router)

    supervisor_modules = frozenset({
        Module.POS, Module.VENTAS, Module.OPERACIONES,
    })
    toggle_stub = AsyncMock(
        return_value={
            "success": True,
            "data": {
                "open_sale_enabled": True,
                "open_sale_product": {"id": str(uuid4()), "name": "Venta libre"},
            },
        }
    )

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.routers.operaciones_context.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=supervisor_modules),
         ), \
         patch(
             "app.routers.operaciones_context.set_open_sale_enabled",
             new=toggle_stub,
         ):
        client = TestClient(app)
        response = client.patch("/operaciones/toggles/open-sale", json={"enabled": True})

    assert response.status_code == 200
    assert response.json()["data"]["open_sale_enabled"] is True
    args, _ = toggle_stub.call_args
    assert args[1] is True


def test_supervisor_updates_tenant_ui_locale_under_enforce():
    """OPERACIONES members can update the locale shared by the tenant."""
    session = _build_session(role="supervisor")
    app = FastAPI()
    app.include_router(operaciones_context_router)
    modules = frozenset({Module.POS, Module.VENTAS, Module.OPERACIONES})
    locale_stub = AsyncMock(
        return_value={"success": True, "data": {"ui_locale": "en"}}
    )

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.routers.operaciones_context.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=modules),
         ), \
         patch(
             "app.routers.operaciones_context.update_ui_locale",
             new=locale_stub,
         ):
        client = TestClient(app)
        response = client.patch("/operaciones/ui-locale", json={"locale": "EN"})

    assert response.status_code == 200
    assert response.json()["data"]["ui_locale"] == "en"
    args, _ = locale_stub.call_args
    assert args == (session.tenant_id, "en")


def test_ui_locale_rejects_unknown_code_before_service():
    session = _build_session(role="supervisor")
    app = FastAPI()
    app.include_router(operaciones_context_router)
    modules = frozenset({Module.OPERACIONES})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.routers.operaciones_context.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=modules),
         ):
        client = TestClient(app)
        response = client.patch("/operaciones/ui-locale", json={"locale": "xx"})

    assert response.status_code == 422


def test_cashier_cannot_update_tenant_ui_locale():
    session = _build_session(role="cashier")
    app = FastAPI()
    app.include_router(operaciones_context_router)

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.routers.operaciones_context.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=frozenset({Module.POS})),
         ):
        client = TestClient(app)
        response = client.patch("/operaciones/ui-locale", json={"locale": "en"})

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_update_toggle_rejects_unknown_column():
    """Whitelist guard: unknown column name raises 422 before any SQL."""
    with pytest.raises(HTTPException) as excinfo:
        await update_toggle(uuid4(), "not_a_real_column", True)

    assert excinfo.value.status_code == 422
    assert "Unknown toggle" in excinfo.value.detail


# ─── Custom mesa label (warocol.com#614) ────────────────────────────────


def test_supervisor_passes_tables_label_under_enforce():
    """Supervisor PATCHes /operaciones/labels/tables with new noun pair."""
    session = _build_session(role="supervisor")
    app = FastAPI()
    app.include_router(operaciones_context_router)

    supervisor_modules = frozenset({
        Module.POS, Module.VENTAS, Module.OPERACIONES,
    })
    label_stub = AsyncMock(return_value={
        "success": True,
        "data": {
            "tables_label_singular": "Habitación",
            "tables_label_plural": "Habitaciones",
        },
    })

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.routers.operaciones_context.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=supervisor_modules),
         ), \
         patch(
             "app.routers.operaciones_context.update_tables_label",
             new=label_stub,
         ):
        client = TestClient(app)
        response = client.patch(
            "/operaciones/labels/tables",
            json={"singular": "Habitación", "plural": "Habitaciones"},
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["tables_label_singular"] == "Habitación"
    assert data["tables_label_plural"] == "Habitaciones"
    # Service receives (tenant_id, singular, plural)
    args, _ = label_stub.call_args
    assert args[1] == "Habitación"
    assert args[2] == "Habitaciones"


def test_cashier_denied_tables_label_under_enforce():
    """Cashier without OPERACIONES cannot PATCH the labels."""
    session = _build_session(role="cashier")
    app = FastAPI()
    app.include_router(operaciones_context_router)

    cashier_modules = frozenset({Module.POS, Module.VENTAS, Module.MENU})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.routers.operaciones_context.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=cashier_modules),
         ):
        client = TestClient(app)
        response = client.patch(
            "/operaciones/labels/tables",
            json={"singular": "Habitación", "plural": "Habitaciones"},
        )

    assert response.status_code == 403
    assert "operaciones" in response.json()["detail"].lower()


def test_tables_label_rejects_oversized_strings():
    """Pydantic max_length=40 enforced before any service code runs."""
    session = _build_session(role="supervisor")
    app = FastAPI()
    app.include_router(operaciones_context_router)

    supervisor_modules = frozenset({
        Module.POS, Module.VENTAS, Module.OPERACIONES,
    })

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.routers.operaciones_context.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=supervisor_modules),
         ):
        client = TestClient(app)
        response = client.patch(
            "/operaciones/labels/tables",
            json={"singular": "x" * 41, "plural": "y" * 41},
        )

    assert response.status_code == 422

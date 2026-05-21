"""Tests for operation events service and router (warocol.com#782)."""
import json
from contextlib import asynccontextmanager
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import permissions
from app.core.middleware import SessionContext
from app.core.permissions import Module
from app.routers.operaciones_operation_events import router as operaciones_operation_events_router
from app.services import operation_events_service


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


def test_serialize_value_handles_uuid_decimal_and_nested_dict():
    uid = uuid4()
    data = {
        "id": uid,
        "price": Decimal("12.50"),
        "items": [{"qty": 2}],
    }
    out = operation_events_service._serialize_value(data)
    assert out["id"] == str(uid)
    assert out["price"] == 12.5
    assert out["items"][0]["qty"] == 2


def test_to_jsonb_round_trip():
    payload = {"product": "Café", "qty": 1, "unit_price": Decimal("5000")}
    raw = operation_events_service._to_jsonb(payload)
    parsed = json.loads(raw)
    assert parsed["product"] == "Café"
    assert parsed["unit_price"] == 5000.0


@pytest.mark.asyncio
async def test_record_operation_event_invalid_action_does_not_execute():
    conn = MagicMock()
    conn.execute = AsyncMock()

    await operation_events_service.record_operation_event(
        conn,
        uuid4(),
        domain="pos",
        channel="mesa",
        action="not_a_real_action",
    )

    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_record_operation_event_inserts_on_valid_input():
    conn = MagicMock()
    conn.execute = AsyncMock()
    tenant_id = uuid4()

    await operation_events_service.record_operation_event(
        conn,
        tenant_id,
        domain="pos",
        channel="mostrador",
        action="cart_line_removed",
        actor_user_id=uuid4(),
        payload={"product_name": "Agua", "qty": 1},
        reason="Error de digitación",
    )

    conn.execute.assert_called_once()
    args = conn.execute.call_args[0]
    assert args[1] == tenant_id
    assert args[4] == "cart_line_removed"


def test_supervisor_passes_list_operation_events_under_enforce():
    session = _build_session(role="supervisor")
    app = FastAPI()
    app.include_router(operaciones_operation_events_router)

    supervisor_modules = frozenset({
        Module.POS, Module.VENTAS, Module.DESPACHO, Module.MENU,
        Module.OPERACIONES, Module.ABASTECIMIENTO, Module.ANALITICA,
    })
    empty = {
        "success": True,
        "data": [],
        "pagination": {"total": 0, "limit": 50, "offset": 0, "has_more": False},
    }

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.services.operation_events_service.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=supervisor_modules),
         ), \
         patch(
             "app.services.operation_events_service.list_operation_events",
             new=AsyncMock(return_value=empty),
         ):
        client = TestClient(app)
        response = client.get("/operaciones/operation-events")

    assert response.status_code == 200
    assert response.json()["success"] is True


def test_cashier_denied_list_operation_events_under_enforce():
    session = _build_session(role="cashier")
    app = FastAPI()
    app.include_router(operaciones_operation_events_router)

    cashier_modules = frozenset({Module.POS, Module.VENTAS, Module.MENU})

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch("app.services.operation_events_service.require_valid_session", return_value=session), \
         patch("app.core.permissions.get_db_connection", side_effect=_enforce_db_ctx()), \
         patch(
             "app.core.permissions.get_role_modules",
             new=AsyncMock(return_value=cashier_modules),
         ):
        client = TestClient(app)
        response = client.get("/operaciones/operation-events")

    assert response.status_code == 403
    assert "operaciones" in response.json()["detail"].lower()

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
        conn.fetchrow = AsyncMock(return_value=None)
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


@pytest.mark.asyncio
async def test_record_operation_event_invalid_domain_does_not_execute():
    conn = MagicMock()
    conn.execute = AsyncMock()

    await operation_events_service.record_operation_event(
        conn,
        uuid4(),
        domain="analitica",
        channel=None,
        action="cart_line_removed",
    )

    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_record_operation_event_rejects_unknown_channel():
    conn = MagicMock()
    conn.execute = AsyncMock()

    await operation_events_service.record_operation_event(
        conn,
        uuid4(),
        domain="pos",
        channel="cocina",
        action="cart_line_removed",
    )

    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_record_operation_event_accepts_non_pos_domain_without_channel():
    conn = MagicMock()
    conn.execute = AsyncMock()
    tenant_id = uuid4()

    await operation_events_service.record_operation_event(
        conn,
        tenant_id,
        domain="ventas",
        channel=None,
        action="cart_line_removed",
        payload={"entity_type": "order", "label": "1001"},
    )

    conn.execute.assert_called_once()
    args = conn.execute.call_args[0]
    assert args[1] == tenant_id
    assert args[2] == "ventas"
    assert args[3] is None


@pytest.mark.asyncio
async def test_list_operation_events_omitted_domain_does_not_filter_domain():
    session = _build_session("owner")
    request = MagicMock()
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=0)
    conn.fetch = AsyncMock(return_value=[])

    @asynccontextmanager
    async def _db(**_kwargs):
        yield conn

    with patch.object(operation_events_service, "require_valid_session", return_value=session), \
         patch.object(operation_events_service, "get_db_connection", _db), \
         patch.object(
             operation_events_service,
             "resolve_tenant_timezone",
             new=AsyncMock(return_value="America/Bogota"),
         ):
        result = await operation_events_service.list_operation_events(request, domain=None)

    assert result["success"] is True
    sql = conn.fetchval.call_args[0][0]
    assert "e.domain =" not in sql


@pytest.mark.asyncio
async def test_list_operation_events_pos_domain_still_filters():
    session = _build_session("owner")
    request = MagicMock()
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=0)
    conn.fetch = AsyncMock(return_value=[])

    @asynccontextmanager
    async def _db(**_kwargs):
        yield conn

    with patch.object(operation_events_service, "require_valid_session", return_value=session), \
         patch.object(operation_events_service, "get_db_connection", _db), \
         patch.object(
             operation_events_service,
             "resolve_tenant_timezone",
             new=AsyncMock(return_value="America/Bogota"),
         ):
        result = await operation_events_service.list_operation_events(request, domain="pos")

    assert result["success"] is True
    sql = conn.fetchval.call_args[0][0]
    assert "e.domain =" in sql
    assert conn.fetchval.call_args[0][2] == "pos"


def test_relax_checks_sql_drops_closed_enums():
    from pathlib import Path

    sql = Path("sql/20260815_operation_events_relax_checks.sql").read_text()
    assert "DROP CONSTRAINT IF EXISTS tenant_operation_events_domain_check" in sql
    assert "DROP CONSTRAINT IF EXISTS tenant_operation_events_channel_check" in sql
    assert "DROP CONSTRAINT IF EXISTS tenant_operation_events_action_check" in sql
    assert "DROP NOT NULL" in sql
    assert "idx_tenant_operation_events_tenant_domain" in sql


@pytest.mark.asyncio
async def test_record_operation_event_accepts_promotion_deleted():
    conn = MagicMock()
    conn.execute = AsyncMock()
    tenant_id = uuid4()

    await operation_events_service.record_operation_event(
        conn,
        tenant_id,
        domain="pos",
        channel="mostrador",
        action="promotion_deleted",
        actor_user_id=uuid4(),
        payload={"promotion_name": "Happy hour"},
        reason="Campaña finalizada",
    )

    conn.execute.assert_called_once()
    args = conn.execute.call_args[0]
    assert args[1] == tenant_id
    assert args[4] == "promotion_deleted"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    [
        "order_status_changed",
        "order_item_deleted",
        "order_item_modifier_deleted",
        "comanda_status_changed",
        "comanda_recalled",
        "comanda_line_cancelled",
    ],
)
async def test_record_operation_event_accepts_ventas_despacho_actions(action):
    conn = MagicMock()
    conn.execute = AsyncMock()
    tenant_id = uuid4()
    order_id = uuid4()

    await operation_events_service.record_operation_event(
        conn,
        tenant_id,
        domain="ventas" if action.startswith("order_") else "despacho",
        channel=None,
        action=action,
        order_id=order_id,
        payload={"entity_type": "order", "entity_id": str(order_id)},
    )

    conn.execute.assert_called_once()
    args = conn.execute.call_args[0]
    assert args[4] == action
    assert args[10] == order_id


@pytest.mark.asyncio
async def test_record_operation_event_accepts_tab_item_edited():
    conn = MagicMock()
    conn.execute = AsyncMock()
    tenant_id = uuid4()

    await operation_events_service.record_operation_event(
        conn,
        tenant_id,
        domain="pos",
        channel="mesa",
        action="tab_item_edited",
        payload={"product_name": "Hamburguesa"},
    )

    conn.execute.assert_called_once()
    assert conn.execute.call_args[0][4] == "tab_item_edited"


@pytest.mark.asyncio
async def test_record_operation_event_accepts_tab_item_edit_blocked():
    conn = MagicMock()
    conn.execute = AsyncMock()
    tenant_id = uuid4()

    await operation_events_service.record_operation_event(
        conn,
        tenant_id,
        domain="pos",
        channel="barra",
        action="tab_item_edit_blocked",
        payload={"product_name": "Hamburguesa"},
    )

    conn.execute.assert_called_once()
    assert conn.execute.call_args[0][4] == "tab_item_edit_blocked"


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

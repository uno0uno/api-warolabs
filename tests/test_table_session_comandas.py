from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.core.exceptions import NotFoundError
from app.services import tables_service


class _DbContext:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_table_session_comandas_returns_delivered_nested_items():
    tenant_id = uuid4()
    table_id = uuid4()
    session_id = uuid4()
    comanda_id = uuid4()
    station_id = uuid4()
    item_id = uuid4()
    order_item_id = uuid4()
    now = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[
        {"id": table_id, "name": "Mesa 1", "status": "open"},
        {"id": session_id, "opened_at": now},
    ])
    conn.fetch = AsyncMock(side_effect=[
        [{
            "id": comanda_id,
            "comanda_number": 25,
            "comanda_index": 2,
            "status": "delivered",
            "source_type": "table",
            "table_display_name": "Mesa 1",
            "notes": None,
            "fired_at": now,
            "preparing_at": None,
            "ready_at": now,
            "delivered_at": now,
            "created_at": now,
            "station_id": station_id,
            "station_name": "Cocina",
            "station_kitchen_name": "Cocina caliente",
            "station_color": "#123456",
        }],
        [{
            "id": item_id,
            "order_item_id": order_item_id,
            "kitchen_name": "Hamburguesa",
            "quantity": Decimal("2"),
            "notes": "Sin cebolla",
            "modifiers_snapshot": '[{"name": "Queso", "quantity": 1}]',
            "status": "ready",
            "ready_at": now,
            "created_at": now,
        }],
    ])

    with patch(
        "app.services.tables_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id),
    ), patch(
        "app.services.tables_service.get_db_connection",
        return_value=_DbContext(conn),
    ):
        result = await tables_service.get_table_session_comandas(object(), table_id)

    assert result["success"] is True
    assert result["data"]["table"]["id"] == str(table_id)
    assert result["data"]["session"]["id"] == str(session_id)

    comanda = result["data"]["comandas"][0]
    assert comanda["id"] == str(comanda_id)
    assert comanda["comanda_number"] == 25
    assert comanda["comanda_index"] == 2
    assert comanda["status"] == "delivered"
    assert comanda["source_type"] == "table"
    assert comanda["station_id"] == str(station_id)
    assert comanda["station_name"] == "Cocina"
    assert comanda["fired_at"] == now.isoformat()

    item = comanda["items"][0]
    assert item["id"] == str(item_id)
    assert item["order_item_id"] == str(order_item_id)
    assert item["kitchen_name"] == "Hamburguesa"
    assert item["quantity"] == 2.0
    assert item["notes"] == "Sin cebolla"
    assert item["modifiers_snapshot"] == [{"name": "Queso", "quantity": 1}]
    assert item["status"] == "ready"

    comandas_query = conn.fetch.await_args_list[0].args[0]
    assert "ts.table_id = $2" in comandas_query
    assert "ts.tenant_id = $3" in comandas_query
    assert "ts.closed_at IS NULL" in comandas_query
    assert "o.tenant_id = $3" in comandas_query
    assert "c.tenant_id = $3" in comandas_query
    assert "'delivered'" in comandas_query
    assert "'cancelled'" not in comandas_query.split("c.status IN", 1)[1].split(")", 1)[0]

    items_query = conn.fetch.await_args_list[1].args[0]
    assert "status != 'cancelled'" in items_query


@pytest.mark.asyncio
async def test_table_session_comandas_requires_open_session():
    tenant_id = uuid4()
    table_id = uuid4()

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[
        {"id": table_id, "name": "Mesa 1", "status": "open"},
        None,
    ])
    conn.fetch = AsyncMock()

    with patch(
        "app.services.tables_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id),
    ), patch(
        "app.services.tables_service.get_db_connection",
        return_value=_DbContext(conn),
    ):
        with pytest.raises(NotFoundError):
            await tables_service.get_table_session_comandas(object(), table_id)

    conn.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_table_session_comandas_uses_tenant_scoped_table_lookup():
    tenant_id = uuid4()
    table_id = uuid4()

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock()

    with patch(
        "app.services.tables_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id),
    ), patch(
        "app.services.tables_service.get_db_connection",
        return_value=_DbContext(conn),
    ):
        with pytest.raises(NotFoundError):
            await tables_service.get_table_session_comandas(object(), table_id)

    table_query = conn.fetchrow.await_args_list[0].args[0]
    assert "tenant_id = $2" in table_query
    assert conn.fetchrow.await_args_list[0].args[1:] == (table_id, tenant_id)
    conn.fetch.assert_not_awaited()

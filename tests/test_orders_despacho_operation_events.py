"""Bitácora CUD writers for ventas / despacho / comandas (warocol.com#2325)."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import Request

from app.services import comandas_service, online_orders_service, orders_service


class _AsyncContext:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _session(tenant_id, user_id):
    return SimpleNamespace(tenant_id=tenant_id, user_id=user_id)


def _capture():
    recorded = []

    async def capture_record(conn, tid, **kwargs):
        recorded.append({"tenant_id": tid, **kwargs})

    return recorded, capture_record


@pytest.mark.asyncio
async def test_update_order_status_cancel_records_ventas_event():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    recorded, capture_record = _capture()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={
        "id": order_id,
        "status": "pending",
        "order_number": 1001,
        "table_session_id": None,
        "pos_cart_id": None,
        "payment_status": None,
        "order_date": datetime(2026, 8, 15),
        "total_amount": 15000,
        "customer_id": None,
    })
    conn.execute = AsyncMock()

    with patch("app.services.orders_service.require_valid_session", return_value=_session(tenant_id, user_id)), \
         patch("app.services.orders_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.orders_service.resolve_tenant_timezone", new=AsyncMock(return_value="America/Bogota")), \
         patch("app.services.orders_service.assert_order_not_in_closed_monthly_period", new=AsyncMock()), \
         patch("app.services.orders_service.record_operation_event", new=capture_record):
        result = await orders_service.update_order_status(
            Request({"type": "http"}),
            order_id,
            "cancelled",
        )

    assert result["success"] is True
    assert len(recorded) == 1
    event = recorded[0]
    assert event["domain"] == "ventas"
    assert event["channel"] is None
    assert event["action"] == "order_status_changed"
    assert event["order_id"] == order_id
    assert event["payload"]["old_status"] == "pending"
    assert event["payload"]["new_status"] == "cancelled"
    assert event["payload"]["order_number"] == 1001


@pytest.mark.asyncio
async def test_update_order_status_same_status_does_not_record():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    recorded, capture_record = _capture()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={
        "id": order_id,
        "status": "pending",
        "order_number": 1001,
        "table_session_id": None,
        "pos_cart_id": None,
        "payment_status": None,
        "order_date": datetime(2026, 8, 15),
        "total_amount": 15000,
        "customer_id": None,
    })
    conn.execute = AsyncMock()

    with patch("app.services.orders_service.require_valid_session", return_value=_session(tenant_id, user_id)), \
         patch("app.services.orders_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.orders_service.resolve_tenant_timezone", new=AsyncMock(return_value="America/Bogota")), \
         patch("app.services.orders_service.assert_order_not_in_closed_monthly_period", new=AsyncMock()), \
         patch("app.services.orders_service.record_operation_event", new=capture_record):
        await orders_service.update_order_status(Request({"type": "http"}), order_id, "pending")

    assert recorded == []


@pytest.mark.asyncio
async def test_delete_order_item_records_event():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    item_id = uuid4()
    recorded, capture_record = _capture()
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_AsyncContext())
    conn.fetchrow = AsyncMock(side_effect=[
        {"id": order_id, "order_number": 42, "order_date": datetime(2026, 8, 15)},
        {"id": item_id, "product_id": uuid4(), "quantity": 2, "product_name": "Bandeja"},
        {"count": 2},
        {"new_total": 10000},
    ])
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()
    snapshot_return = AsyncMock(return_value=True)

    with patch("app.services.orders_service.require_valid_session", return_value=_session(tenant_id, user_id)), \
         patch("app.services.orders_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.orders_service.assert_order_not_in_closed_monthly_period", new=AsyncMock()), \
         patch(
             "app.services.orders_service._pos_modifier_inventory_helpers",
             return_value=(AsyncMock(), AsyncMock(), snapshot_return),
         ), \
         patch("app.services.orders_service.record_operation_event", new=capture_record):
        result = await orders_service.delete_order_item(Request({"type": "http"}), order_id, item_id)

    assert result["success"] is True
    assert len(recorded) == 1
    event = recorded[0]
    assert event["domain"] == "ventas"
    assert event["action"] == "order_item_deleted"
    assert event["order_id"] == order_id
    assert event["order_item_id"] == item_id
    assert event["payload"]["product_name"] == "Bandeja"


@pytest.mark.asyncio
async def test_delete_order_item_modifier_records_event():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    item_id = uuid4()
    modifier_id = uuid4()
    recorded, capture_record = _capture()
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_AsyncContext())
    conn.fetchrow = AsyncMock(side_effect=[
        {"id": order_id, "order_number": 42, "order_date": datetime(2026, 8, 15)},
        {"id": item_id, "quantity": 1, "product_name": "Hamburguesa"},
        {
            "id": modifier_id,
            "price_at_purchase": 2000,
            "modifier_name": "Queso",
            "modifier_qty": 1,
            "included_quantity_at_purchase": 0,
            "original_modifier_id": None,
            "ingredient_id": None,
            "ingredient_quantity": None,
            "ingredient_unit": None,
            "ingredient_name": None,
        },
        {"new_subtotal": 12000},
        {"new_total": 12000},
    ])
    conn.execute = AsyncMock()

    with patch("app.services.orders_service.require_valid_session", return_value=_session(tenant_id, user_id)), \
         patch("app.services.orders_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.orders_service.assert_order_not_in_closed_monthly_period", new=AsyncMock()), \
         patch(
             "app.services.orders_service._pos_modifier_inventory_helpers",
             return_value=(AsyncMock(), AsyncMock(), AsyncMock()),
         ), \
         patch("app.services.orders_service.record_operation_event", new=capture_record):
        result = await orders_service.delete_order_item_modifier(
            Request({"type": "http"}),
            order_id,
            item_id,
            modifier_id,
        )

    assert result["success"] is True
    assert len(recorded) == 1
    event = recorded[0]
    assert event["action"] == "order_item_modifier_deleted"
    assert event["domain"] == "ventas"
    assert event["order_id"] == order_id
    assert event["payload"]["modifier_name"] == "Queso"


@pytest.mark.asyncio
async def test_online_update_order_status_records_despacho_event():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    recorded, capture_record = _capture()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[
        {
            "id": order_id,
            "status": "pending",
            "customer_id": None,
            "payment_method": None,
            "payment_method_id": None,
            "order_number": 77,
        },
        {"change_date": datetime(2026, 8, 15, tzinfo=timezone.utc)},
    ])
    conn.execute = AsyncMock()

    with patch("app.services.online_orders_service.require_valid_session", return_value=_session(tenant_id, user_id)), \
         patch("app.services.online_orders_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.online_orders_service.resolve_tenant_timezone", new=AsyncMock(return_value="America/Bogota")), \
         patch("app.services.online_orders_service.record_operation_event", new=capture_record):
        result = await online_orders_service.update_order_status(
            Request({"type": "http"}),
            order_id,
            "confirmed",
        )

    assert result["success"] is True
    assert len(recorded) == 1
    event = recorded[0]
    assert event["domain"] == "despacho"
    assert event["channel"] is None
    assert event["action"] == "order_status_changed"
    assert event["order_id"] == order_id
    assert event["payload"]["new_status"] == "confirmed"


@pytest.mark.asyncio
async def test_get_order_status_history_does_not_record():
    tenant_id = uuid4()
    order_id = uuid4()
    recorded, capture_record = _capture()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"id": order_id})
    conn.fetch = AsyncMock(return_value=[])

    with patch("app.services.online_orders_service.require_valid_session", return_value=_session(tenant_id, uuid4())), \
         patch("app.services.online_orders_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.online_orders_service.record_operation_event", new=capture_record):
        result = await online_orders_service.get_order_status_history(
            Request({"type": "http"}),
            order_id,
        )

    assert result["success"] is True
    assert recorded == []


@pytest.mark.asyncio
async def test_update_comanda_status_records_event():
    tenant_id = uuid4()
    user_id = uuid4()
    comanda_id = uuid4()
    order_id = uuid4()
    recorded, capture_record = _capture()
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=True)
    conn.fetchrow = AsyncMock(return_value={
        "id": comanda_id,
        "status": "pending",
        "source_type": "table",
        "is_bar": False,
        "order_id": order_id,
    })
    conn.execute = AsyncMock()

    with patch("app.services.comandas_service.require_valid_session", return_value=_session(tenant_id, user_id)), \
         patch("app.services.comandas_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.comandas_service.record_operation_event", new=capture_record):
        result = await comandas_service.update_comanda_status(
            Request({"type": "http"}),
            comanda_id,
            "preparing",
        )

    assert result["success"] is True
    assert len(recorded) == 1
    event = recorded[0]
    assert event["domain"] == "despacho"
    assert event["action"] == "comanda_status_changed"
    assert event["order_id"] == order_id
    assert event["payload"]["old_status"] == "pending"
    assert event["payload"]["new_status"] == "preparing"


@pytest.mark.asyncio
async def test_update_comanda_status_noop_does_not_record():
    tenant_id = uuid4()
    comanda_id = uuid4()
    recorded, capture_record = _capture()
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=True)
    conn.fetchrow = AsyncMock(return_value={
        "id": comanda_id,
        "status": "ready",
        "source_type": "table",
        "is_bar": False,
        "order_id": uuid4(),
    })
    conn.execute = AsyncMock()

    with patch("app.services.comandas_service.require_valid_session", return_value=_session(tenant_id, uuid4())), \
         patch("app.services.comandas_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.comandas_service.record_operation_event", new=capture_record):
        result = await comandas_service.update_comanda_status(
            Request({"type": "http"}),
            comanda_id,
            "ready",
        )

    assert result.get("noop") is True
    assert recorded == []
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_bulk_update_comanda_status_records_one_per_changed_row():
    tenant_id = uuid4()
    changed_id = uuid4()
    stale_id = uuid4()
    order_id = uuid4()
    recorded, capture_record = _capture()
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=True)
    conn.fetch = AsyncMock(return_value=[
        {
            "id": changed_id,
            "status": "pending",
            "source_type": "table",
            "is_bar": False,
            "order_id": order_id,
        },
        {
            "id": stale_id,
            "status": "preparing",
            "source_type": "table",
            "is_bar": False,
            "order_id": order_id,
        },
    ])
    conn.execute = AsyncMock()

    with patch("app.services.comandas_service.require_valid_session", return_value=_session(tenant_id, uuid4())), \
         patch("app.services.comandas_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.comandas_service.record_operation_event", new=capture_record):
        result = await comandas_service.bulk_update_comanda_status(
            Request({"type": "http"}),
            [changed_id, stale_id],
            "preparing",
        )

    assert result["success"] is True
    assert len(recorded) == 1
    assert recorded[0]["payload"]["entity_id"] == str(changed_id)
    assert recorded[0]["action"] == "comanda_status_changed"


@pytest.mark.asyncio
async def test_recall_comanda_records_event():
    tenant_id = uuid4()
    comanda_id = uuid4()
    order_id = uuid4()
    recorded, capture_record = _capture()
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=True)
    conn.fetchrow = AsyncMock(return_value={
        "id": comanda_id,
        "status": "delivered",
        "delivered_at": datetime.now(timezone.utc),
        "order_id": order_id,
    })
    conn.execute = AsyncMock()

    with patch("app.services.comandas_service.require_valid_session", return_value=_session(tenant_id, uuid4())), \
         patch("app.services.comandas_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.comandas_service.record_operation_event", new=capture_record):
        result = await comandas_service.recall_comanda(Request({"type": "http"}), comanda_id)

    assert result["success"] is True
    assert len(recorded) == 1
    assert recorded[0]["action"] == "comanda_recalled"
    assert recorded[0]["order_id"] == order_id
    assert recorded[0]["domain"] == "despacho"


@pytest.mark.asyncio
async def test_update_comanda_item_cancelled_records_line_event():
    tenant_id = uuid4()
    comanda_id = uuid4()
    item_id = uuid4()
    order_id = uuid4()
    order_item_id = uuid4()
    recorded, capture_record = _capture()
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_AsyncContext())
    conn.fetchval = AsyncMock(side_effect=[True, 1])
    conn.fetchrow = AsyncMock(return_value={
        "id": item_id,
        "comanda_id": comanda_id,
        "order_item_id": order_item_id,
        "current_status": "pending",
        "order_id": order_id,
    })
    conn.execute = AsyncMock()

    with patch("app.services.comandas_service.require_valid_session", return_value=_session(tenant_id, uuid4())), \
         patch("app.services.comandas_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.comandas_service.record_operation_event", new=capture_record):
        result = await comandas_service.update_comanda_item_status(
            Request({"type": "http"}),
            comanda_id,
            item_id,
            "cancelled",
        )

    assert result["success"] is True
    assert len(recorded) == 1
    event = recorded[0]
    assert event["action"] == "comanda_line_cancelled"
    assert event["order_id"] == order_id
    assert event["comanda_item_id"] == item_id


@pytest.mark.asyncio
async def test_update_comanda_item_ready_does_not_record():
    tenant_id = uuid4()
    comanda_id = uuid4()
    item_id = uuid4()
    recorded, capture_record = _capture()
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_AsyncContext())
    conn.fetchval = AsyncMock(side_effect=[True, 1])
    conn.fetchrow = AsyncMock(return_value={
        "id": item_id,
        "comanda_id": comanda_id,
        "order_item_id": uuid4(),
        "current_status": "pending",
        "order_id": uuid4(),
    })
    conn.execute = AsyncMock()

    with patch("app.services.comandas_service.require_valid_session", return_value=_session(tenant_id, uuid4())), \
         patch("app.services.comandas_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.comandas_service.record_operation_event", new=capture_record):
        result = await comandas_service.update_comanda_item_status(
            Request({"type": "http"}),
            comanda_id,
            item_id,
            "ready",
        )

    assert result["success"] is True
    assert recorded == []

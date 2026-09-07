"""comanda_items.notes copied from order_items on fire (#757)."""
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.services.comandas_service import _fire_with_conn


@pytest.mark.asyncio
async def test_fire_comandas_copies_order_item_notes_to_comanda_items():
    tenant_id = uuid4()
    order_id = uuid4()
    order_item_id = uuid4()
    product_id = uuid4()
    station_id = uuid4()
    comanda_id = uuid4()

    mock_conn = AsyncMock()

    order_rows = [
        {
            "id": order_item_id,
            "quantity": 1,
            "product_id": product_id,
            "notes": "Sin cebolla",
            "kitchen_name": "Hamburguesa",
        },
    ]

    async def fetch_side_effect(query, *args):
        if "order_item_modifiers" in query:
            return []
        return order_rows

    mock_conn.fetch = AsyncMock(side_effect=fetch_side_effect)

    fetchrow_calls = []

    async def fetchrow_side_effect(query, *args):
        fetchrow_calls.append((query, args))
        q = " ".join(query.split())
        if "INSERT INTO comandas" in q:
            return {
                "id": comanda_id,
                "comanda_number": 1,
                "comanda_index": 1,
                "station_id": station_id,
                "status": "pending",
                "source_type": "table",
                "table_display_name": "Mesa 1",
                "notes": None,
                "fired_at": None,
                "ready_at": None,
                "created_at": None,
            }
        if "INSERT INTO comanda_items" in q:
            return {
                "id": uuid4(),
                "order_item_id": order_item_id,
                "kitchen_name": "Hamburguesa",
                "quantity": 1,
                "notes": args[5] if len(args) > 5 else None,
                "modifiers_snapshot": None,
                "status": "pending",
                "ready_at": None,
                "created_at": None,
            }
        if "SELECT order_number" in q:
            return 42
        if "SELECT COUNT" in q and "comandas" in q:
            return 1
        if "SELECT name FROM kitchen_stations" in q:
            return "Parrilla"
        return None

    mock_conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effect)
    mock_conn.fetchval = AsyncMock(side_effect=lambda q, *a: 42 if "order_number" in q else 1)
    mock_conn.execute = AsyncMock()

    with patch(
        "app.services.comandas_service.get_effective_station",
        new_callable=AsyncMock,
        return_value=station_id,
    ):
        result = await _fire_with_conn(
            mock_conn,
            order_id,
            tenant_id,
            "table",
            "Mesa 1",
            None,
        )

    assert len(result) == 1
    assert result[0]["items"][0]["notes"] == "Sin cebolla"

    insert_calls = [c for c in fetchrow_calls if "INSERT INTO comanda_items" in " ".join(c[0].split())]
    assert len(insert_calls) == 1
    assert insert_calls[0][1][5] == "Sin cebolla"


@pytest.mark.asyncio
async def test_fire_comandas_returns_printable_fallback_for_item_without_station():
    tenant_id = uuid4()
    order_id = uuid4()
    order_item_id = uuid4()
    product_id = uuid4()

    mock_conn = AsyncMock()

    order_rows = [
        {
            "id": order_item_id,
            "quantity": 1,
            "product_id": product_id,
            "notes": "prueba de paoas en nota",
            "kitchen_name": "Santa inquisicion",
        },
    ]

    async def fetch_side_effect(query, *args):
        if "order_item_modifiers" in query:
            return [
                {
                    "modifier_name": "Extra salsa",
                    "price_at_purchase": 2500,
                    "quantity": 2,
                },
            ]
        return order_rows

    mock_conn.fetch = AsyncMock(side_effect=fetch_side_effect)
    mock_conn.fetchrow = AsyncMock()
    mock_conn.fetchval = AsyncMock(
        side_effect=lambda q, *a: 42 if "order_number" in q else 1
    )
    mock_conn.execute = AsyncMock()

    with patch(
        "app.services.comandas_service.get_effective_station",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = await _fire_with_conn(
            mock_conn,
            order_id,
            tenant_id,
            "table",
            "Mesa 1",
            None,
        )

    assert len(result) == 1
    fallback = result[0]
    assert fallback["station_id"] is None
    assert fallback["station_name"] == "Sin cocina asignada"
    assert fallback["print_fallback"] is True
    assert fallback["comanda_number"] == 42

    item = fallback["items"][0]
    assert item["order_item_id"] == order_item_id
    assert item["kitchen_name"] == "Santa inquisicion"
    assert item["quantity"] == 1.0
    assert item["notes"] == "prueba de paoas en nota"
    assert item["modifiers_snapshot"] == [
        {"name": "Extra salsa", "price": 2500.0, "quantity": 2}
    ]

    mock_conn.fetchrow.assert_not_awaited()
    update_call = next(
        call for call in mock_conn.execute.await_args_list
        if "UPDATE order_items" in call.args[0]
    )
    update_sql = " ".join(update_call.args[0].split())
    assert "UPDATE order_items SET fulfillment_status = 'sent'" in update_sql
    assert update_call.args[1] == [order_item_id]


@pytest.mark.asyncio
async def test_fire_comandas_keeps_station_group_and_adds_unrouted_fallback():
    tenant_id = uuid4()
    order_id = uuid4()
    routed_item_id = uuid4()
    unrouted_item_id = uuid4()
    routed_product_id = uuid4()
    unrouted_product_id = uuid4()
    station_id = uuid4()
    comanda_id = uuid4()

    mock_conn = AsyncMock()

    order_rows = [
        {
            "id": routed_item_id,
            "quantity": 1,
            "product_id": routed_product_id,
            "notes": None,
            "kitchen_name": "Hamburguesa",
        },
        {
            "id": unrouted_item_id,
            "quantity": 1,
            "product_id": unrouted_product_id,
            "notes": "sin papa",
            "kitchen_name": "Santa inquisicion",
        },
    ]

    async def fetch_side_effect(query, *args):
        if "order_item_modifiers" in query:
            return []
        return order_rows

    mock_conn.fetch = AsyncMock(side_effect=fetch_side_effect)

    async def fetchrow_side_effect(query, *args):
        q = " ".join(query.split())
        if "INSERT INTO comandas" in q:
            return {
                "id": comanda_id,
                "comanda_number": 42,
                "comanda_index": 1,
                "station_id": station_id,
                "status": "pending",
                "source_type": "table",
                "table_display_name": "Mesa 1",
                "notes": None,
                "fired_at": None,
                "ready_at": None,
                "created_at": None,
            }
        if "INSERT INTO comanda_items" in q:
            return {
                "id": uuid4(),
                "order_item_id": routed_item_id,
                "kitchen_name": "Hamburguesa",
                "quantity": args[3],
                "notes": args[5] if len(args) > 5 else None,
                "modifiers_snapshot": args[4] if len(args) > 4 else None,
                "status": "pending",
                "ready_at": None,
                "created_at": None,
            }
        return None

    mock_conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effect)

    def fetchval_side_effect(query, *args):
        if "order_number" in query:
            return 42
        if "SELECT name FROM kitchen_stations" in query:
            return "Parrilla"
        return 2

    mock_conn.fetchval = AsyncMock(side_effect=fetchval_side_effect)
    mock_conn.execute = AsyncMock()

    async def station_side_effect(product_id, *_args):
        if product_id == routed_product_id:
            return station_id
        return None

    with patch(
        "app.services.comandas_service.get_effective_station",
        new_callable=AsyncMock,
        side_effect=station_side_effect,
    ):
        result = await _fire_with_conn(
            mock_conn,
            order_id,
            tenant_id,
            "table",
            "Mesa 1",
            None,
        )

    assert len(result) == 2
    routed, fallback = result
    assert routed["station_id"] == station_id
    assert routed["station_name"] == "Parrilla"
    assert routed["items"][0]["order_item_id"] == routed_item_id

    assert fallback["station_id"] is None
    assert fallback["station_name"] == "Sin cocina asignada"
    assert fallback["print_fallback"] is True
    assert fallback["items"][0]["order_item_id"] == unrouted_item_id
    assert fallback["items"][0]["notes"] == "sin papa"

    # SSE printable order: station first, fallback last (#1973)
    from app.services.notifications_service import build_comanda_fired_print_payload

    slim = build_comanda_fired_print_payload(result)
    assert slim[0]["print_fallback"] is False
    assert slim[-1]["print_fallback"] is True

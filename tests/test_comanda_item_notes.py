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

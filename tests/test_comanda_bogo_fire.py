"""BOGO bundle semantics when firing comandas (warocol.com#1021)."""
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.services.comandas_service import (
    _bogo_comanda_kitchen_lines,
    _comanda_kitchen_lines_for_order_item,
    _fire_with_conn,
)


def test_bogo_3x1_splits_paid_and_free_units():
    lines = _bogo_comanda_kitchen_lines(
        3,
        buy_qty=2,
        get_qty=1,
        promotion_name="3×1 Pizza",
        base_notes=None,
    )
    assert len(lines) == 2
    assert lines[0] == {"quantity": 2, "notes": None, "is_promo_free": False}
    assert lines[1]["quantity"] == 1
    assert lines[1]["is_promo_free"] is True
    assert "GRATIS (3×1 Pizza)" in lines[1]["notes"]
    assert sum(line["quantity"] for line in lines) == 3


def test_bogo_partial_bundle_keeps_single_row_without_gratis():
    lines = _bogo_comanda_kitchen_lines(
        2,
        buy_qty=2,
        get_qty=1,
        promotion_name="3×1",
        base_notes="Extra queso",
    )
    assert lines == [{"quantity": 2, "notes": "Extra queso", "is_promo_free": False}]


def test_comanda_kitchen_lines_non_bogo_unchanged():
    item = {
        "quantity": 2,
        "notes": "Sin cebolla",
        "promo_type": "percent_off",
        "applied_promotion_id": uuid4(),
    }
    assert _comanda_kitchen_lines_for_order_item(item) == [
        {"quantity": 2.0, "notes": "Sin cebolla", "is_promo_free": False},
    ]


@pytest.mark.asyncio
async def test_fire_comandas_bogo_emits_paid_and_free_comanda_items():
    tenant_id = uuid4()
    order_id = uuid4()
    order_item_id = uuid4()
    product_id = uuid4()
    station_id = uuid4()
    comanda_id = uuid4()
    promo_id = uuid4()

    order_rows = [
        {
            "id": order_item_id,
            "quantity": 3,
            "product_id": product_id,
            "notes": None,
            "applied_promotion_id": promo_id,
            "promo_savings_allocated": 10000,
            "promo_type": "bogo",
            "promotion_name": "3×1 Promo",
            "promotion_value_json": {"buy_qty": 2, "get_qty": 1},
            "kitchen_name": "Pizza Especial",
        },
    ]

    mock_conn = AsyncMock()

    async def fetch_side_effect(query, *args):
        if "order_item_modifiers" in query:
            return [
                {
                    "modifier_name": "Borde queso",
                    "price_at_purchase": 2000,
                    "quantity": 1,
                },
            ]
        return order_rows

    mock_conn.fetch = AsyncMock(side_effect=fetch_side_effect)

    insert_calls = []

    async def fetchrow_side_effect(query, *args):
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
            insert_calls.append(args)
            return {
                "id": uuid4(),
                "order_item_id": order_item_id,
                "kitchen_name": "Pizza Especial",
                "quantity": args[3],
                "notes": args[5] if len(args) > 5 else None,
                "modifiers_snapshot": args[4] if len(args) > 4 else None,
                "status": "pending",
                "ready_at": None,
                "created_at": None,
            }
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
    items = result[0]["items"]
    assert len(items) == 2
    assert sum(int(i["quantity"]) for i in items) == 3
    assert items[0]["is_promo_free"] is False
    assert items[1]["is_promo_free"] is True
    assert "GRATIS (3×1 Promo)" in items[1]["notes"]
    assert all(i["modifiers_snapshot"][0]["name"] == "Borde queso" for i in items)

    assert len(insert_calls) == 2
    assert insert_calls[0][3] == 2
    assert insert_calls[1][3] == 1
    assert "GRATIS (3×1 Promo)" in insert_calls[1][5]

"""COGS snapshot aggregation when product recipe and modifier share an ingredient (#321)."""
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.services import pos_cart_service


CARNE_INGREDIENT_ID = uuid4()
PRODUCT_ID = uuid4()
ORDER_ITEM_ID = uuid4()
MODIFIER_ID = uuid4()
TENANT_ID = uuid4()
BASE_RECIPE_SOURCE_ID = uuid4()


def _product_recipe_row(quantity: float = 150.0):
    return {
        "source_id": str(BASE_RECIPE_SOURCE_ID),
        "source_type": "PRODUCT_RECIPE",
        "ingredient_id": CARNE_INGREDIENT_ID,
        "ingredient_name": "Carne de Res",
        "quantity": quantity,
        "unit": "gr",
    }


def _modifier_ingredient_row(quantity: float = 150.0):
    return {
        "ingredient_id": CARNE_INGREDIENT_ID,
        "ingredient_quantity": quantity,
        "ingredient_unit": "gr",
        "ingredient_name": "Carne de Res",
        "controla_inventario": True,
    }


@pytest.mark.asyncio
async def test_capture_helpers_use_aggregate_upsert_sql():
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=[_product_recipe_row()])
    mock_conn.execute = AsyncMock()

    with patch(
        "app.services.pos_cart_service.resolve_recipe_quantity_to_base_unit",
        new=AsyncMock(return_value=150.0),
    ), patch(
        "app.services.pos_cart_service._get_last_purchase_prices",
        new=AsyncMock(return_value={str(CARNE_INGREDIENT_ID): 10.0}),
    ):
        await pos_cart_service._capture_order_item_ingredients(
            mock_conn,
            ORDER_ITEM_ID,
            PRODUCT_ID,
            1.0,
            str(TENANT_ID),
        )

    product_sql = mock_conn.execute.await_args.args[0]
    assert "order_item_ingredients.quantity + EXCLUDED.quantity" in product_sql
    assert "DO NOTHING" not in product_sql

    mock_conn.reset_mock()
    with patch(
        "app.services.pos_cart_service.resolve_recipe_quantity_to_base_unit",
        new=AsyncMock(return_value=150.0),
    ), patch(
        "app.services.pos_cart_service._get_last_purchase_prices",
        new=AsyncMock(return_value={str(CARNE_INGREDIENT_ID): 10.0}),
    ):
        await pos_cart_service._capture_modifier_ingredient_snapshot(
            mock_conn,
            ORDER_ITEM_ID,
            _modifier_ingredient_row(),
            MODIFIER_ID,
            1.0,
            1.0,
            str(TENANT_ID),
        )

    modifier_sql = mock_conn.execute.await_args.args[0]
    assert "order_item_ingredients.quantity + EXCLUDED.quantity" in modifier_sql
    assert "DO NOTHING" not in modifier_sql


@pytest.mark.asyncio
async def test_modifier_then_product_snapshot_totals_300gr_carne():
    """Santa inquisición base + Carne adición: 150gr + 150gr aggregated snapshot."""
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=[_product_recipe_row()])
    mock_conn.execute = AsyncMock()
    captured = []

    async def record_execute(query, *args):
        captured.append((query, args))

    mock_conn.execute = AsyncMock(side_effect=record_execute)

    with patch(
        "app.services.pos_cart_service.resolve_recipe_quantity_to_base_unit",
        new=AsyncMock(return_value=150.0),
    ), patch(
        "app.services.pos_cart_service._get_last_purchase_prices",
        new=AsyncMock(return_value={str(CARNE_INGREDIENT_ID): 10.0}),
    ):
        await pos_cart_service._capture_modifier_ingredient_snapshot(
            mock_conn,
            ORDER_ITEM_ID,
            _modifier_ingredient_row(),
            MODIFIER_ID,
            1.0,
            1.0,
            str(TENANT_ID),
        )
        await pos_cart_service._capture_order_item_ingredients(
            mock_conn,
            ORDER_ITEM_ID,
            PRODUCT_ID,
            1.0,
            str(TENANT_ID),
        )

    assert len(captured) == 2
    modifier_args = captured[0][1]
    product_args = captured[1][1]
    assert modifier_args[3] == pytest.approx(150.0)
    assert modifier_args[6] == pytest.approx(1500.0)
    assert product_args[3] == pytest.approx(150.0)
    assert product_args[6] == pytest.approx(1500.0)


@pytest.mark.asyncio
async def test_modifier_snapshot_multiplies_modifier_qty():
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()

    with patch(
        "app.services.pos_cart_service.resolve_recipe_quantity_to_base_unit",
        new=AsyncMock(return_value=150.0),
    ), patch(
        "app.services.pos_cart_service._get_last_purchase_prices",
        new=AsyncMock(return_value={str(CARNE_INGREDIENT_ID): 10.0}),
    ):
        await pos_cart_service._capture_modifier_ingredient_snapshot(
            mock_conn,
            ORDER_ITEM_ID,
            _modifier_ingredient_row(),
            MODIFIER_ID,
            1.0,
            2.0,
            str(TENANT_ID),
        )

    args = mock_conn.execute.await_args.args
    assert args[4] == pytest.approx(300.0)
    assert args[7] == pytest.approx(3000.0)


@pytest.mark.asyncio
async def test_same_source_retry_sends_replace_branch_in_sql():
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=[_product_recipe_row()])
    mock_conn.execute = AsyncMock()

    with patch(
        "app.services.pos_cart_service.resolve_recipe_quantity_to_base_unit",
        new=AsyncMock(return_value=150.0),
    ), patch(
        "app.services.pos_cart_service._get_last_purchase_prices",
        new=AsyncMock(return_value={str(CARNE_INGREDIENT_ID): 10.0}),
    ):
        await pos_cart_service._capture_order_item_ingredients(
            mock_conn, ORDER_ITEM_ID, PRODUCT_ID, 1.0, str(TENANT_ID),
        )
        await pos_cart_service._capture_order_item_ingredients(
            mock_conn, ORDER_ITEM_ID, PRODUCT_ID, 1.0, str(TENANT_ID),
        )

    sql = mock_conn.execute.await_args.args[0]
    assert "THEN EXCLUDED.quantity" in sql
    assert mock_conn.execute.await_count == 2

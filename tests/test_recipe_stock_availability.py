"""Unit tests for recipe-stock catalog visibility (warocol.com#2574)."""
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.recipe_stock_availability_service import (
    is_hide_products_without_stock_enabled,
    product_ids_insufficient_recipe_stock,
    resolve_recipe_qty_with_meta,
)


def test_resolve_same_unit_unchanged():
    assert resolve_recipe_qty_with_meta(2, "gr", "gr", None) == 2.0


def test_resolve_gr_to_und():
    assert resolve_recipe_qty_with_meta(360, "gr", "und", 180) == 2.0


@pytest.mark.asyncio
async def test_flag_off_when_null():
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=None)
    assert await is_hide_products_without_stock_enabled(conn, uuid4()) is False


@pytest.mark.asyncio
async def test_flag_on_only_when_true():
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=True)
    assert await is_hide_products_without_stock_enabled(conn, uuid4()) is True


@pytest.mark.asyncio
async def test_insufficient_product_ids_when_stock_low():
    tenant_id = uuid4()
    product_id = uuid4()
    ingredient_id = uuid4()

    conn = MagicMock()
    conn.fetch = AsyncMock(
        side_effect=[
            [
                {
                    "product_id": product_id,
                    "ingredient_id": ingredient_id,
                    "quantity": 2,
                    "unit": "und",
                }
            ],
            [{"id": ingredient_id, "unit": "und", "unit_weight_gr": None}],
            [{"ingredient_id": ingredient_id, "current_stock": 1}],
        ]
    )

    result = await product_ids_insufficient_recipe_stock(conn, tenant_id)
    assert result == {product_id}


@pytest.mark.asyncio
async def test_sufficient_stock_not_hidden():
    tenant_id = uuid4()
    product_id = uuid4()
    ingredient_id = uuid4()

    conn = MagicMock()
    conn.fetch = AsyncMock(
        side_effect=[
            [
                {
                    "product_id": product_id,
                    "ingredient_id": ingredient_id,
                    "quantity": 1,
                    "unit": "und",
                }
            ],
            [{"id": ingredient_id, "unit": "und", "unit_weight_gr": None}],
            [{"ingredient_id": ingredient_id, "current_stock": 5}],
        ]
    )

    result = await product_ids_insufficient_recipe_stock(conn, tenant_id)
    assert result == set()


@pytest.mark.asyncio
async def test_missing_inventory_row_treated_as_zero():
    tenant_id = uuid4()
    product_id = uuid4()
    ingredient_id = uuid4()

    conn = MagicMock()
    conn.fetch = AsyncMock(
        side_effect=[
            [
                {
                    "product_id": product_id,
                    "ingredient_id": ingredient_id,
                    "quantity": 1,
                    "unit": "und",
                }
            ],
            [{"id": ingredient_id, "unit": "und", "unit_weight_gr": None}],
            [],
        ]
    )

    result = await product_ids_insufficient_recipe_stock(conn, tenant_id)
    assert result == {product_id}

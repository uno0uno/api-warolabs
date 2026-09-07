"""Online menu soft-hide when hide_products_without_stock is on (warocol.com#2575)."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.services import public_restaurant_service
from app.services.recipe_stock_availability_service import (
    apply_hide_products_without_stock_filter,
)


@pytest.mark.asyncio
async def test_get_menu_by_tenant_id_unchanged_when_flag_off():
    tenant_id = uuid4()
    product_id = uuid4()
    category_id = uuid4()

    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"display_name": "Demo Rest"})
    conn.fetch = AsyncMock(
        side_effect=[
            [{"id": category_id, "name": "Bebidas", "description": None}],
            [{
                "id": product_id,
                "name": "Café",
                "description": None,
                "price": 5000,
                "image_url": None,
                "category_id": category_id,
                "category_name": "Bebidas",
                "is_available": True,
                "preparation_time": 5,
                "allow_modifiers": False,
                "has_modifiers": False,
            }],
        ]
    )

    @asynccontextmanager
    async def _ctx():
        yield conn

    with patch(
        "app.services.public_restaurant_service.get_db_connection",
        side_effect=_ctx,
    ), patch(
        "app.services.recipe_stock_availability_service.is_hide_products_without_stock_enabled",
        new=AsyncMock(return_value=False),
    ), patch(
        "app.services.recipe_stock_availability_service.product_ids_insufficient_recipe_stock",
        new=AsyncMock(return_value={product_id}),
    ) as hide_ids:
        result = await public_restaurant_service.get_menu_by_tenant_id(tenant_id)

    assert len(result["products"]) == 1
    assert result["products"][0]["id"] == product_id
    hide_ids.assert_not_awaited()
    product_query = conn.fetch.await_args_list[1].args[0]
    assert "<> ALL" not in product_query


@pytest.mark.asyncio
async def test_get_menu_by_tenant_id_excludes_insufficient_when_flag_on():
    tenant_id = uuid4()
    keep_id = uuid4()
    hide_id = uuid4()
    category_id = uuid4()

    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"display_name": "Demo Rest"})
    conn.fetch = AsyncMock(
        side_effect=[
            [{"id": category_id, "name": "Bebidas", "description": None}],
            [{
                "id": keep_id,
                "name": "Té",
                "description": None,
                "price": 3000,
                "image_url": None,
                "category_id": category_id,
                "category_name": "Bebidas",
                "is_available": True,
                "preparation_time": 3,
                "allow_modifiers": False,
                "has_modifiers": False,
            }],
        ]
    )

    @asynccontextmanager
    async def _ctx():
        yield conn

    with patch(
        "app.services.public_restaurant_service.get_db_connection",
        side_effect=_ctx,
    ), patch(
        "app.services.recipe_stock_availability_service.is_hide_products_without_stock_enabled",
        new=AsyncMock(return_value=True),
    ), patch(
        "app.services.recipe_stock_availability_service.product_ids_insufficient_recipe_stock",
        new=AsyncMock(return_value={hide_id}),
    ):
        result = await public_restaurant_service.get_menu_by_tenant_id(tenant_id)

    assert len(result["products"]) == 1
    assert result["products"][0]["id"] == keep_id
    product_call = conn.fetch.await_args_list[1]
    product_query = product_call.args[0]
    assert "<> ALL" in product_query
    assert hide_id in product_call.args[2]


@pytest.mark.asyncio
async def test_apply_filter_no_op_when_hide_set_empty():
    tenant_id = uuid4()
    conn = MagicMock()

    with patch(
        "app.services.recipe_stock_availability_service.is_hide_products_without_stock_enabled",
        new=AsyncMock(return_value=True),
    ), patch(
        "app.services.recipe_stock_availability_service.product_ids_insufficient_recipe_stock",
        new=AsyncMock(return_value=set()),
    ):
        query, params = await apply_hide_products_without_stock_filter(
            conn,
            tenant_id,
            "SELECT 1 FROM product p WHERE p.tenant_id = $1",
            [tenant_id],
        )

    assert "<> ALL" not in query
    assert params == [tenant_id]

"""Sale edit + COGS paths for composite modifier options (#1124)."""
from datetime import date
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.services import pos_cart_service
from app.services.cierre_service import _post_order_cogs_gl_entry

ORDER_ITEM_ID = uuid4()
MODIFIER_ID = uuid4()
ING_A = uuid4()
ING_B = uuid4()
TENANT_ID = uuid4()
ORDER_ID = uuid4()
USER_ID = uuid4()


@pytest.mark.asyncio
async def test_subtract_snapshot_deletes_row_when_quantity_zero():
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value={"quantity": 50.0, "unit_cost": 2.0})
    mock_conn.execute = AsyncMock()

    await pos_cart_service._subtract_order_item_ingredient_snapshot(
        mock_conn, ORDER_ITEM_ID, ING_A, 50.0
    )

    sql = mock_conn.execute.await_args.args[0]
    assert "DELETE FROM order_item_ingredients" in sql


@pytest.mark.asyncio
async def test_subtract_snapshot_reduces_quantity_and_total_cost():
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value={"quantity": 100.0, "unit_cost": 3.0})
    mock_conn.execute = AsyncMock()

    await pos_cart_service._subtract_order_item_ingredient_snapshot(
        mock_conn, ORDER_ITEM_ID, ING_A, 40.0
    )

    sql = mock_conn.execute.await_args.args[0]
    assert "UPDATE order_item_ingredients" in sql
    assert mock_conn.execute.await_args.args[3] == pytest.approx(60.0)
    assert mock_conn.execute.await_args.args[4] == pytest.approx(180.0)


@pytest.mark.asyncio
async def test_return_modifier_inventory_reverses_composite_lines():
    mock_conn = AsyncMock()
    lines = [
        {
            "ingredient_id": ING_A,
            "quantity": 10.0,
            "unit": "gr",
            "ingredient_name": "Queso",
            "controla_inventario": True,
        },
        {
            "ingredient_id": ING_B,
            "quantity": 5.0,
            "unit": "gr",
            "ingredient_name": "Tocineta",
            "controla_inventario": True,
        },
    ]

    with patch(
        "app.services.modifier_option_service.resolve_modifier_ingredient_lines",
        new=AsyncMock(return_value=lines),
    ), patch(
        "app.services.orders_service._return_ingredient_to_stock",
        new=AsyncMock(),
    ) as mock_return, patch.object(
        pos_cart_service,
        "_subtract_order_item_ingredient_snapshot",
        new=AsyncMock(),
    ) as mock_subtract:
        await pos_cart_service.return_modifier_inventory_for_order_item(
            mock_conn,
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            order_id=ORDER_ID,
            order_number=42,
            order_item_id=ORDER_ITEM_ID,
            item_quantity=2.0,
            modifier_id=MODIFIER_ID,
            modifier_qty=1.0,
            modifier_name="Extra",
            product_name="Hamburguesa",
        )

    assert mock_return.await_count == 2
    assert mock_subtract.await_count == 2
    first_return_qty = mock_return.await_args_list[0].args[6]
    assert first_return_qty == pytest.approx(20.0)


@pytest.mark.asyncio
async def test_cogs_gl_query_sums_order_item_ingredients():
    mock_conn = AsyncMock()
    mock_conn.fetchval = AsyncMock(side_effect=[None, 2500.0])
    mock_conn.fetchrow = AsyncMock(return_value=None)

    await _post_order_cogs_gl_entry(
        mock_conn,
        TENANT_ID,
        ORDER_ID,
        date(2026, 6, 2),
        order_number=99,
    )

    cogs_query = mock_conn.fetchval.await_args_list[1].args[0]
    assert "order_item_ingredients" in cogs_query
    assert "SUM(oii.total_cost)" in cogs_query
    assert mock_conn.fetchval.await_args_list[1].args[1] == ORDER_ID

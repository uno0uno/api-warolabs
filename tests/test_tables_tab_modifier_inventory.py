"""Mesa/bar tab modifier inventory deduction (#319)."""
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.services import pos_cart_service, tables_service


@pytest.mark.asyncio
async def test_add_tab_items_core_deducts_modifier_inventory():
    tenant_id = uuid4()
    user_id = uuid4()
    table_id = uuid4()
    session_id = uuid4()
    order_id = uuid4()
    product_id = uuid4()
    order_item_id = uuid4()
    modifier_id = uuid4()

    session_row = {
        "session_id": session_id,
        "table_name": "Mesa 3",
        "is_bar": False,
        "effective_waiter_member_id": None,
    }
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(side_effect=[
        session_row,
        None,
        {"id": order_id, "order_number": 42, "total_amount": 43.0},
        {"id": order_item_id},
        {"total_amount": 43.0},
    ])
    mock_conn.fetch = AsyncMock(return_value=[])
    mock_conn.execute = AsyncMock()

    items = [{
        "product_id": product_id,
        "quantity": 1,
        "unit_price": 25.0,
        "modifiers": [{
            "id": str(modifier_id),
            "name": "Tocineta",
            "price": 4.0,
        }],
    }]

    pricing_map = {str(product_id): {"price": Decimal("25.00"), "open_priced": False}}
    deduct_mock = AsyncMock()

    with patch(
        "app.services.tables_service._record_tab_operation_event",
        new=AsyncMock(),
    ), patch(
        "app.services.tables_service._prefetch_product_names",
        new=AsyncMock(return_value={str(product_id): "Santa inquisición"}),
    ), patch(
        "app.services.tables_service.fetch_product_pricing_map",
        new=AsyncMock(return_value=pricing_map),
    ), patch(
        "app.services.tables_service._capture_order_item_ingredients",
        new=AsyncMock(),
    ), patch(
        "app.services.tables_service._deduct_modifier_inventory_for_order_item",
        deduct_mock,
    ):
        await tables_service._add_tab_items_core(
            mock_conn, tenant_id, user_id, table_id, items
        )

    deduct_mock.assert_called_once()
    call_kwargs = deduct_mock.call_args.kwargs
    assert call_kwargs["order_item_id"] == order_item_id
    assert call_kwargs["modifier"]["name"] == "Tocineta"
    assert call_kwargs["modifier_qty"] == 1.0


@pytest.mark.asyncio
async def test_add_tab_items_core_persists_modifier_quantity():
    tenant_id = uuid4()
    user_id = uuid4()
    table_id = uuid4()
    session_id = uuid4()
    order_id = uuid4()
    product_id = uuid4()
    order_item_id = uuid4()
    modifier_id = uuid4()

    session_row = {
        "session_id": session_id,
        "table_name": "Mesa 3",
        "is_bar": False,
        "effective_waiter_member_id": None,
    }
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(side_effect=[
        session_row,
        None,
        {"id": order_id, "order_number": 42, "total_amount": 61.0},
        {"id": order_item_id},
        {"total_amount": 52.0},
    ])
    mock_conn.fetch = AsyncMock(return_value=[])
    mock_conn.execute = AsyncMock()

    items = [{
        "product_id": product_id,
        "quantity": 1,
        "unit_price": 25.0,
        "modifiers": [{
            "id": str(modifier_id),
            "name": "Carne de Res",
            "price": 9.0,
            "quantity": 3,
        }],
    }]

    pricing_map = {str(product_id): {"price": Decimal("25.00"), "open_priced": False}}

    with patch(
        "app.services.tables_service._record_tab_operation_event",
        new=AsyncMock(),
    ), patch(
        "app.services.tables_service._prefetch_product_names",
        new=AsyncMock(return_value={str(product_id): "Santa inquisición"}),
    ), patch(
        "app.services.tables_service.fetch_product_pricing_map",
        new=AsyncMock(return_value=pricing_map),
    ), patch(
        "app.services.tables_service._capture_order_item_ingredients",
        new=AsyncMock(),
    ), patch(
        "app.services.tables_service._deduct_modifier_inventory_for_order_item",
        new=AsyncMock(),
    ):
        await tables_service._add_tab_items_core(
            mock_conn, tenant_id, user_id, table_id, items
        )

    insert_call = mock_conn.execute.call_args_list[0]
    assert insert_call.args[5] == 3

    order_item_insert = mock_conn.fetchrow.call_args_list[3]
    assert order_item_insert.args[5] == 25.0 + 9.0 * 3


@pytest.mark.asyncio
async def test_remove_tab_item_returns_inventory_from_snapshots():
    tenant_id = uuid4()
    user_id = uuid4()
    table_id = uuid4()
    order_item_id = uuid4()
    order_id = uuid4()
    product_id = uuid4()

    row = {
        "id": order_item_id,
        "product_id": product_id,
        "quantity": 1,
        "price_at_purchase": 25.0,
        "subtotal": 43.0,
        "notes": None,
        "fulfillment_status": "new",
        "order_id": order_id,
        "total_amount": 43.0,
        "order_number": 99,
        "table_session_id": uuid4(),
        "product_name": "Santa inquisición",
        "table_name": "Mesa 1",
        "is_bar": False,
        "effective_waiter_member_id": None,
    }

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(side_effect=[row, None])
    mock_conn.execute = AsyncMock()
    return_mock = AsyncMock()

    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_cm.__aexit__ = AsyncMock(return_value=None)

    mock_request = MagicMock()
    with patch(
        "app.services.tables_service.require_valid_session",
    ) as mock_sess, patch(
        "app.services.tables_service.get_db_connection",
        return_value=mock_cm,
    ), patch(
        "app.services.tables_service._fetch_order_item_modifiers",
        new=AsyncMock(return_value=[]),
    ), patch(
        "app.services.tables_service._record_tab_operation_event",
        new=AsyncMock(),
    ), patch(
        "app.services.tables_service._return_tab_item_inventory_from_snapshots",
        return_mock,
    ):
        mock_sess.return_value = MagicMock(
            tenant_id=tenant_id, user_id=user_id,
        )
        await tables_service.remove_tab_item(
            mock_request, table_id, order_item_id
        )

    return_mock.assert_called_once()
    assert return_mock.call_args.kwargs["order_item_id"] == order_item_id


@pytest.mark.asyncio
async def test_deduct_modifier_inventory_for_order_item_writes_movement():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    order_item_id = uuid4()
    modifier_id = uuid4()
    ingredient_id = uuid4()

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(side_effect=[
        {
            "option_type": "INGREDIENT",
            "ingredient_id": ingredient_id,
            "ingredient_quantity": 1,
            "ingredient_unit": "und",
            "ingredient_name": "Tocineta",
            "controla_inventario": True,
        },
        {"current_stock": 10.0},
    ])
    mock_conn.execute = AsyncMock()

    with patch(
        "app.services.modifier_option_service.resolve_recipe_quantity_to_base_unit",
        new=AsyncMock(return_value=1.0),
    ), patch(
        "app.services.pos_cart_service._capture_modifier_ingredient_line_snapshot",
        new=AsyncMock(),
    ) as snapshot_mock:
        await pos_cart_service._deduct_modifier_inventory_for_order_item(
            mock_conn,
            tenant_id=tenant_id,
            user_id=user_id,
            order_id=order_id,
            order_item_id=order_item_id,
            order_number=100,
            item_quantity=1.0,
            modifier={"id": modifier_id, "name": "Tocineta"},
            modifier_qty=1.0,
        )

    assert mock_conn.execute.await_count >= 2
    snapshot_mock.assert_called_once()

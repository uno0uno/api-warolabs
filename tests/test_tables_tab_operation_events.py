"""Operation audit events for mesa/barra tab flows (warocol.com#783, #786)."""
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.core.exceptions import APIError
from app.services import tables_service


@pytest.mark.asyncio
async def test_add_tab_items_core_records_tab_item_added_per_line():
    tenant_id = uuid4()
    user_id = uuid4()
    table_id = uuid4()
    session_id = uuid4()
    order_id = uuid4()
    product_id = uuid4()
    order_item_id = uuid4()
    recorded = []

    async def capture_record(conn, tid, **kwargs):
        recorded.append(kwargs)

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
        {"id": order_id, "order_number": 42, "total_amount": 15.0},
        {"id": order_item_id},
    ])
    mock_conn.fetch = AsyncMock(return_value=[])
    mock_conn.execute = AsyncMock()

    items = [{
        "product_id": product_id,
        "quantity": 1,
        "unit_price": 15.0,
        "modifiers": [{"id": None, "name": "Extra", "price": 0.0}],
        "notes": "Sin cebolla",
    }]

    pricing_map = {
        str(product_id): {"price": Decimal("15.00"), "open_priced": False},
    }
    with patch(
        "app.services.tables_service._record_tab_operation_event",
        side_effect=capture_record,
    ), patch(
        "app.services.tables_service._prefetch_product_names",
        new=AsyncMock(return_value={str(product_id): "Hamburguesa"}),
    ), patch(
        "app.services.tables_service.fetch_product_pricing_map",
        new=AsyncMock(return_value=pricing_map),
    ), patch(
        "app.services.tables_service._capture_order_item_ingredients",
        new=AsyncMock(),
    ):
        result = await tables_service._add_tab_items_core(
            mock_conn, tenant_id, user_id, table_id, items
        )

    assert result["session_id"] == session_id
    assert len(recorded) == 1
    assert recorded[0]["action"] == "tab_item_added"
    assert recorded[0]["tab_ctx"]["channel"] == "mesa"
    assert recorded[0]["tab_ctx"]["table_session_id"] == session_id
    assert recorded[0]["payload"]["product_name"] == "Hamburguesa"
    assert recorded[0]["payload"]["order_number"] == 42


@pytest.mark.asyncio
async def test_remove_tab_item_records_before_delete():
    tenant_id = uuid4()
    user_id = uuid4()
    table_id = uuid4()
    order_item_id = uuid4()
    order_id = uuid4()
    session_id = uuid4()
    product_id = uuid4()
    recorded = []
    delete_calls = []

    async def capture_record(conn, tid, **kwargs):
        recorded.append(kwargs)

    row = {
        "id": order_item_id,
        "product_id": product_id,
        "quantity": 2,
        "price_at_purchase": 10.0,
        "subtotal": 20.0,
        "notes": None,
        "order_id": order_id,
        "total_amount": 20.0,
        "order_number": 7,
        "table_session_id": session_id,
        "product_name": "Pizza",
        "table_name": "Barra 1",
        "is_bar": True,
        "effective_waiter_member_id": None,
        "fulfillment_status": "new",
    }

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(side_effect=[row, None])
    mock_conn.fetch = AsyncMock(return_value=[])

    async def track_execute(sql, *args):
        if "DELETE FROM order_items" in sql:
            delete_calls.append(True)

    mock_conn.execute = AsyncMock(side_effect=track_execute)

    class _Txn:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *args):
            return None

    mock_conn.transaction = MagicMock(return_value=_Txn())
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_cm.__aexit__ = AsyncMock(return_value=None)

    mock_request = MagicMock()
    with patch("app.services.tables_service.require_valid_session") as mock_sess, \
         patch("app.services.tables_service.get_db_connection", return_value=mock_cm), \
         patch(
             "app.services.tables_service._record_tab_operation_event",
             side_effect=capture_record,
         ):
        mock_sess.return_value = MagicMock(
            tenant_id=tenant_id, user_id=user_id,
        )
        await tables_service.remove_tab_item(
            mock_request, table_id, order_item_id,
        )

    assert len(recorded) == 1
    assert recorded[0]["action"] == "tab_item_removed"
    assert recorded[0]["tab_ctx"]["channel"] == "barra"
    assert recorded[0]["comanda_item_id"] is None
    assert recorded[0]["payload"]["product_name"] == "Pizza"
    assert len(delete_calls) == 1


@pytest.mark.asyncio
async def test_add_then_remove_shares_table_session_id():
    """Integration-style: two events, same table_session_id (#783)."""
    tenant_id = uuid4()
    user_id = uuid4()
    table_id = uuid4()
    session_id = uuid4()
    recorded = []

    async def capture_record(conn, tid, **kwargs):
        recorded.append(kwargs)

    # --- add (core only) ---
    order_id = uuid4()
    product_id = uuid4()
    order_item_id = uuid4()
    mock_conn_add = AsyncMock()
    mock_conn_add.fetchrow = AsyncMock(side_effect=[
        {
            "session_id": session_id,
            "table_name": "Mesa 1",
            "is_bar": False,
            "effective_waiter_member_id": None,
        },
        None,
        {"id": order_id, "order_number": 1, "total_amount": 5.0},
        {"id": order_item_id},
    ])
    mock_conn_add.fetch = AsyncMock(return_value=[])
    mock_conn_add.execute = AsyncMock()

    with patch(
        "app.services.tables_service._record_tab_operation_event",
        side_effect=capture_record,
    ), patch(
        "app.services.tables_service._prefetch_product_names",
        new=AsyncMock(return_value={str(product_id): "Agua"}),
    ), patch(
        "app.services.tables_service._capture_order_item_ingredients",
        new=AsyncMock(),
    ):
        await tables_service._add_tab_items_core(
            mock_conn_add, tenant_id, user_id, table_id,
            [{"product_id": product_id, "quantity": 1, "unit_price": 5.0, "modifiers": []}],
        )

    # --- remove ---
    row = {
        "id": order_item_id,
        "product_id": product_id,
        "quantity": 1,
        "price_at_purchase": 5.0,
        "subtotal": 5.0,
        "notes": None,
        "order_id": order_id,
        "total_amount": 5.0,
        "order_number": 1,
        "table_session_id": session_id,
        "product_name": "Agua",
        "table_name": "Mesa 1",
        "is_bar": False,
        "effective_waiter_member_id": None,
        "fulfillment_status": "new",
    }
    mock_conn_rm = AsyncMock()
    mock_conn_rm.fetchrow = AsyncMock(side_effect=[row, None])
    mock_conn_rm.fetch = AsyncMock(return_value=[])
    mock_conn_rm.execute = AsyncMock()

    class _Txn:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *args):
            return None

    mock_conn_rm.transaction = MagicMock(return_value=_Txn())
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_conn_rm)
    mock_cm.__aexit__ = AsyncMock(return_value=None)

    with patch("app.services.tables_service.require_valid_session") as mock_sess, \
         patch("app.services.tables_service.get_db_connection", return_value=mock_cm), \
         patch(
             "app.services.tables_service._record_tab_operation_event",
             side_effect=capture_record,
         ):
        mock_sess.return_value = MagicMock(
            tenant_id=tenant_id, user_id=user_id,
        )
        await tables_service.remove_tab_item(
            MagicMock(), table_id, order_item_id,
        )

    assert len(recorded) == 2
    assert recorded[0]["action"] == "tab_item_added"
    assert recorded[1]["action"] == "tab_item_removed"
    assert recorded[0]["tab_ctx"]["table_session_id"] == session_id
    assert recorded[1]["tab_ctx"]["table_session_id"] == session_id


@pytest.mark.asyncio
async def test_remove_fired_item_without_reason_raises_400():
    tenant_id = uuid4()
    user_id = uuid4()
    table_id = uuid4()
    order_item_id = uuid4()
    order_id = uuid4()
    session_id = uuid4()

    row = {
        "id": order_item_id,
        "product_id": uuid4(),
        "quantity": 1,
        "price_at_purchase": 10.0,
        "subtotal": 10.0,
        "notes": None,
        "fulfillment_status": "sent",
        "order_id": order_id,
        "total_amount": 10.0,
        "order_number": 3,
        "table_session_id": session_id,
        "product_name": "Pasta",
        "table_name": "Mesa 2",
        "is_bar": False,
        "effective_waiter_member_id": None,
    }

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(side_effect=[row, None])
    mock_conn.fetch = AsyncMock(return_value=[])
    mock_conn.execute = AsyncMock()

    class _Txn:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *args):
            return None

    mock_conn.transaction = MagicMock(return_value=_Txn())
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_cm.__aexit__ = AsyncMock(return_value=None)

    with patch("app.services.tables_service.require_valid_session") as mock_sess, \
         patch("app.services.tables_service.get_db_connection", return_value=mock_cm):
        mock_sess.return_value = MagicMock(tenant_id=tenant_id, user_id=user_id)
        with pytest.raises(APIError) as exc:
            await tables_service.remove_tab_item(
                MagicMock(), table_id, order_item_id, reason="",
            )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_remove_fired_item_with_reason_records_event():
    tenant_id = uuid4()
    user_id = uuid4()
    table_id = uuid4()
    order_item_id = uuid4()
    order_id = uuid4()
    session_id = uuid4()
    comanda_item_id = uuid4()
    recorded = []

    async def capture_record(conn, tid, **kwargs):
        recorded.append(kwargs)

    row = {
        "id": order_item_id,
        "product_id": uuid4(),
        "quantity": 1,
        "price_at_purchase": 15.0,
        "subtotal": 15.0,
        "notes": None,
        "fulfillment_status": "preparing",
        "order_id": order_id,
        "total_amount": 15.0,
        "order_number": 9,
        "table_session_id": session_id,
        "product_name": "Sopa",
        "table_name": "Mesa 4",
        "is_bar": False,
        "effective_waiter_member_id": None,
    }
    comanda_row = {"id": comanda_item_id, "comanda_id": uuid4()}

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(side_effect=[row, comanda_row])
    mock_conn.fetch = AsyncMock(return_value=[])
    mock_conn.fetchval = AsyncMock(return_value=1)
    mock_conn.execute = AsyncMock()

    class _Txn:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *args):
            return None

    mock_conn.transaction = MagicMock(return_value=_Txn())
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_cm.__aexit__ = AsyncMock(return_value=None)

    with patch("app.services.tables_service.require_valid_session") as mock_sess, \
         patch("app.services.tables_service.get_db_connection", return_value=mock_cm), \
         patch(
             "app.services.tables_service._record_tab_operation_event",
             side_effect=capture_record,
         ):
        mock_sess.return_value = MagicMock(tenant_id=tenant_id, user_id=user_id)
        await tables_service.remove_tab_item(
            MagicMock(),
            table_id,
            order_item_id,
            reason="  Cliente cambió de opinión  ",
        )

    assert len(recorded) == 1
    assert recorded[0]["reason"] == "Cliente cambió de opinión"
    assert recorded[0]["comanda_item_id"] == comanda_item_id

from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import Request

from app.core.exceptions import APIError
from app.routers.orders import ManualOrderModifier
from app.services import cierre_service, orders_service


class _AsyncContext:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _manual_order_conn(order_id, order_item_id, order_date):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "id": order_id,
                "order_number": 14798,
                "order_date": order_date,
                "created_at": order_date,
            },
            {"id": order_item_id},
        ]
    )
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()
    conn.transaction = MagicMock(return_value=_AsyncContext())
    return conn


@pytest.mark.asyncio
async def test_update_order_status_finalizes_pending_pos_with_gl_cogs_without_double_stock():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    pos_cart_id = uuid4()
    customer_id = uuid4()
    payment_method_id = uuid4()
    group_id = uuid4()
    order_date = datetime(2026, 6, 16, 14, 30)

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "id": order_id,
                "status": "pending",
                "order_number": 8521,
                "table_session_id": None,
                "pos_cart_id": pos_cart_id,
                "payment_status": None,
                "order_date": order_date,
                "total_amount": 42000,
                "customer_id": customer_id,
            },
            {"id": group_id},
            {"id": payment_method_id},
            {
                "id": order_id,
                "order_number": 8521,
                "total_amount": 42000,
                "payment_method": "card",
                "payment_method_id": payment_method_id,
                "order_date": order_date,
                "tip_amount": 0,
                "tip_tax_amount": 0,
            },
        ]
    )
    conn.execute = AsyncMock()

    deduct_stock = AsyncMock()
    post_gl = AsyncMock()
    post_cogs = AsyncMock()

    with patch(
        "app.services.orders_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id, user_id=user_id),
    ), patch(
        "app.services.orders_service.get_db_connection",
        return_value=_AsyncContext(conn),
    ), patch(
        "app.services.orders_service.assert_order_not_in_closed_monthly_period",
        new=AsyncMock(),
    ), patch(
        "app.services.orders_service._deduct_stock_for_status_update",
        new=deduct_stock,
    ), patch(
        "app.services.orders_service._get_tenant_tax_config",
        new=AsyncMock(return_value={"inc_enabled": True}),
    ), patch(
        "app.services.orders_service._post_order_gl_entry",
        new=post_gl,
    ), patch(
        "app.services.orders_service._post_order_cogs_gl_entry",
        new=post_cogs,
    ), patch(
        "app.services.orders_service.evaluate_and_award",
        new=MagicMock(return_value=object()),
    ) as award_waros, patch(
        "app.services.orders_service.asyncio.create_task",
        new=MagicMock(),
    ) as create_task:
        result = await orders_service.update_order_status(
            Request({"type": "http"}),
            order_id,
            "completed",
            "card",
            str(payment_method_id),
        )

    assert result["success"] is True
    deduct_stock.assert_not_awaited()
    post_gl.assert_awaited_once()
    assert post_gl.await_args.kwargs["order_id"] == order_id
    assert post_gl.await_args.kwargs["payment_method"] == "card"
    assert post_gl.await_args.kwargs["payment_method_id"] == payment_method_id
    update_args = conn.execute.await_args_list[0].args
    assert update_args[2] == "card"
    assert update_args[4] == payment_method_id
    assert update_args[6] == "paid"
    post_cogs.assert_awaited_once_with(
        conn=conn,
        tenant_id=tenant_id,
        order_id=order_id,
        order_date=date(2026, 6, 16),
        order_number=8521,
    )
    award_waros.assert_called_once_with(order_id, customer_id, tenant_id)
    create_task.assert_called_once()


@pytest.mark.asyncio
async def test_update_order_status_finalizes_pending_table_with_gl_cogs_without_double_stock():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    table_session_id = uuid4()
    customer_id = uuid4()
    payment_method_id = uuid4()
    group_id = uuid4()
    order_date = datetime(2026, 6, 16, 14, 30)

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "id": order_id,
                "status": "pending",
                "order_number": 8522,
                "table_session_id": table_session_id,
                "pos_cart_id": None,
                "payment_status": None,
                "order_date": order_date,
                "total_amount": 52000,
                "customer_id": customer_id,
            },
            {"id": group_id},
            {"id": payment_method_id},
            {
                "id": order_id,
                "order_number": 8522,
                "total_amount": 52000,
                "payment_method": "card",
                "payment_method_id": payment_method_id,
                "order_date": order_date,
                "tip_amount": 0,
                "tip_tax_amount": 0,
            },
        ]
    )
    conn.fetchval = AsyncMock(return_value=True)
    conn.execute = AsyncMock()

    deduct_stock = AsyncMock()
    post_gl = AsyncMock()
    post_cogs = AsyncMock()

    with patch(
        "app.services.orders_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id, user_id=user_id),
    ), patch(
        "app.services.orders_service.get_db_connection",
        return_value=_AsyncContext(conn),
    ), patch(
        "app.services.orders_service.assert_order_not_in_closed_monthly_period",
        new=AsyncMock(),
    ), patch(
        "app.services.orders_service._deduct_stock_for_status_update",
        new=deduct_stock,
    ), patch(
        "app.services.orders_service._get_tenant_tax_config",
        new=AsyncMock(return_value={"inc_enabled": True}),
    ), patch(
        "app.services.orders_service._post_order_gl_entry",
        new=post_gl,
    ), patch(
        "app.services.orders_service._post_order_cogs_gl_entry",
        new=post_cogs,
    ), patch(
        "app.services.orders_service.evaluate_and_award",
        new=MagicMock(return_value=object()),
    ) as award_waros, patch(
        "app.services.orders_service.asyncio.create_task",
        new=MagicMock(),
    ) as create_task:
        result = await orders_service.update_order_status(
            Request({"type": "http"}),
            order_id,
            "completed",
            "card",
            str(payment_method_id),
        )

    assert result["success"] is True
    conn.fetchval.assert_awaited_once()
    deduct_stock.assert_not_awaited()
    post_gl.assert_awaited_once()
    post_cogs.assert_awaited_once_with(
        conn=conn,
        tenant_id=tenant_id,
        order_id=order_id,
        order_date=date(2026, 6, 16),
        order_number=8522,
    )
    award_waros.assert_called_once_with(order_id, customer_id, tenant_id)
    create_task.assert_called_once()


@pytest.mark.asyncio
async def test_update_order_status_deducts_stock_for_pending_table_without_consumption_movements():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    table_session_id = uuid4()
    customer_id = uuid4()
    group_id = uuid4()
    order_date = datetime(2026, 6, 16, 14, 30)

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "id": order_id,
                "status": "pending",
                "order_number": 8523,
                "table_session_id": table_session_id,
                "pos_cart_id": None,
                "payment_status": None,
                "order_date": order_date,
                "total_amount": 62000,
                "customer_id": customer_id,
            },
            {"id": group_id},
            {
                "id": order_id,
                "order_number": 8523,
                "total_amount": 62000,
                "payment_method": "cash",
                "payment_method_id": None,
                "order_date": order_date,
                "tip_amount": 0,
                "tip_tax_amount": 0,
            },
        ]
    )
    conn.fetchval = AsyncMock(return_value=False)
    conn.execute = AsyncMock()

    deduct_stock = AsyncMock()

    with patch(
        "app.services.orders_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id, user_id=user_id),
    ), patch(
        "app.services.orders_service.get_db_connection",
        return_value=_AsyncContext(conn),
    ), patch(
        "app.services.orders_service.assert_order_not_in_closed_monthly_period",
        new=AsyncMock(),
    ), patch(
        "app.services.orders_service._deduct_stock_for_status_update",
        new=deduct_stock,
    ), patch(
        "app.services.orders_service._get_tenant_tax_config",
        new=AsyncMock(return_value={"inc_enabled": True}),
    ), patch(
        "app.services.orders_service._post_order_gl_entry",
        new=AsyncMock(),
    ), patch(
        "app.services.orders_service._post_order_cogs_gl_entry",
        new=AsyncMock(),
    ), patch(
        "app.services.orders_service.evaluate_and_award",
        new=MagicMock(return_value=object()),
    ), patch(
        "app.services.orders_service.asyncio.create_task",
        new=MagicMock(),
    ):
        result = await orders_service.update_order_status(
            Request({"type": "http"}),
            order_id,
            "completed",
            "cash",
        )

    assert result["success"] is True
    conn.fetchval.assert_awaited_once()
    deduct_stock.assert_awaited_once_with(conn, order_id, tenant_id, user_id, 8523)


def _pending_status_conn(order_id, order_date, customer_id=None, *, group_row=None, method_row=None):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "id": order_id,
                "status": "pending",
                "order_number": 8521,
                "table_session_id": None,
                "pos_cart_id": uuid4(),
                "payment_status": None,
                "order_date": order_date,
                "total_amount": 42000,
                "customer_id": customer_id,
            },
            group_row,
            method_row,
        ]
    )
    conn.execute = AsyncMock()
    return conn


@pytest.mark.asyncio
async def test_update_order_status_requires_payment_method_with_method_id():
    tenant_id = uuid4()
    order_id = uuid4()
    conn = _pending_status_conn(order_id, datetime(2026, 6, 16, 14, 30))

    with patch(
        "app.services.orders_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id, user_id=uuid4()),
    ), patch(
        "app.services.orders_service.get_db_connection",
        return_value=_AsyncContext(conn),
    ), patch(
        "app.services.orders_service.assert_order_not_in_closed_monthly_period",
        new=AsyncMock(),
    ):
        with pytest.raises(APIError) as exc:
            await orders_service.update_order_status(
                Request({"type": "http"}),
                order_id,
                "completed",
                payment_method_id=str(uuid4()),
            )

    assert exc.value.status_code == 400
    assert exc.value.details["code"] == "payment_method_required"
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_order_status_accepts_global_payment_group_with_tenant_method():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    customer_id = uuid4()
    payment_method_id = uuid4()
    global_group_id = uuid4()
    order_date = datetime(2026, 6, 16, 14, 30)

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "id": order_id,
                "status": "pending",
                "order_number": 8524,
                "table_session_id": None,
                "pos_cart_id": uuid4(),
                "payment_status": None,
                "order_date": order_date,
                "total_amount": 200,
                "customer_id": customer_id,
            },
            {"id": global_group_id},
            {"id": payment_method_id},
            {
                "id": order_id,
                "order_number": 8524,
                "total_amount": 200,
                "payment_method": "digital",
                "payment_method_id": payment_method_id,
                "order_date": order_date,
                "tip_amount": 0,
                "tip_tax_amount": 0,
            },
        ]
    )
    conn.execute = AsyncMock()

    post_gl = AsyncMock()
    post_cogs = AsyncMock()

    with patch(
        "app.services.orders_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id, user_id=user_id),
    ), patch(
        "app.services.orders_service.get_db_connection",
        return_value=_AsyncContext(conn),
    ), patch(
        "app.services.orders_service.assert_order_not_in_closed_monthly_period",
        new=AsyncMock(),
    ), patch(
        "app.services.orders_service._deduct_stock_for_status_update",
        new=AsyncMock(),
    ), patch(
        "app.services.orders_service._get_tenant_tax_config",
        new=AsyncMock(return_value={"inc_enabled": True}),
    ), patch(
        "app.services.orders_service._post_order_gl_entry",
        new=post_gl,
    ), patch(
        "app.services.orders_service._post_order_cogs_gl_entry",
        new=post_cogs,
    ), patch(
        "app.services.orders_service.evaluate_and_award",
        new=MagicMock(return_value=object()),
    ), patch(
        "app.services.orders_service.asyncio.create_task",
        new=MagicMock(),
    ):
        result = await orders_service.update_order_status(
            Request({"type": "http"}),
            order_id,
            "completed",
            "digital",
            str(payment_method_id),
        )

    assert result["success"] is True
    group_query = conn.fetchrow.await_args_list[1].args[0]
    assert "tenant_id IS NULL" in group_query
    update_args = conn.execute.await_args_list[0].args
    assert update_args[2] == "digital"
    assert update_args[4] == payment_method_id
    post_gl.assert_awaited_once()
    assert post_gl.await_args.kwargs["payment_method_id"] == payment_method_id
    post_cogs.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_order_status_rejects_invalid_method_group_pair():
    tenant_id = uuid4()
    order_id = uuid4()
    group_id = uuid4()
    method_id = uuid4()
    conn = _pending_status_conn(
        order_id,
        datetime(2026, 6, 16, 14, 30),
        group_row={"id": group_id},
        method_row=None,
    )

    with patch(
        "app.services.orders_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id, user_id=uuid4()),
    ), patch(
        "app.services.orders_service.get_db_connection",
        return_value=_AsyncContext(conn),
    ), patch(
        "app.services.orders_service.assert_order_not_in_closed_monthly_period",
        new=AsyncMock(),
    ):
        with pytest.raises(APIError) as exc:
            await orders_service.update_order_status(
                Request({"type": "http"}),
                order_id,
                "completed",
                "digital",
                str(method_id),
            )

    assert exc.value.status_code == 400
    assert exc.value.details["code"] == "payment_method_id_invalid"
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_order_status_rejects_invalid_payment_group():
    tenant_id = uuid4()
    order_id = uuid4()
    conn = _pending_status_conn(
        order_id,
        datetime(2026, 6, 16, 14, 30),
        group_row=None,
    )

    with patch(
        "app.services.orders_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id, user_id=uuid4()),
    ), patch(
        "app.services.orders_service.get_db_connection",
        return_value=_AsyncContext(conn),
    ), patch(
        "app.services.orders_service.assert_order_not_in_closed_monthly_period",
        new=AsyncMock(),
    ):
        with pytest.raises(APIError) as exc:
            await orders_service.update_order_status(
                Request({"type": "http"}),
                order_id,
                "completed",
                "not-a-group",
                str(uuid4()),
            )

    assert exc.value.status_code == 400
    assert exc.value.details["code"] == "payment_method_invalid"
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_order_status_sets_credit_payment_status():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    customer_id = uuid4()
    order_date = datetime(2026, 6, 16, 14, 30)

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "id": order_id,
                "status": "pending",
                "order_number": 8521,
                "table_session_id": None,
                "pos_cart_id": uuid4(),
                "payment_status": None,
                "order_date": order_date,
                "total_amount": 42000,
                "customer_id": customer_id,
            },
            {"id": uuid4()},
            {
                "id": order_id,
                "order_number": 8521,
                "total_amount": 42000,
                "payment_method": "credit",
                "payment_method_id": None,
                "order_date": order_date,
                "tip_amount": 0,
                "tip_tax_amount": 0,
            },
        ]
    )
    conn.execute = AsyncMock()

    with patch(
        "app.services.orders_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id, user_id=user_id),
    ), patch(
        "app.services.orders_service.get_db_connection",
        return_value=_AsyncContext(conn),
    ), patch(
        "app.services.orders_service.assert_order_not_in_closed_monthly_period",
        new=AsyncMock(),
    ), patch(
        "app.services.orders_service._deduct_stock_for_status_update",
        new=AsyncMock(),
    ), patch(
        "app.services.orders_service._get_tenant_tax_config",
        new=AsyncMock(return_value={"inc_enabled": True}),
    ), patch(
        "app.services.orders_service._post_order_gl_entry",
        new=AsyncMock(),
    ), patch(
        "app.services.orders_service._post_order_cogs_gl_entry",
        new=AsyncMock(),
    ):
        result = await orders_service.update_order_status(
            Request({"type": "http"}),
            order_id,
            "completed",
            "credit",
        )

    assert result["success"] is True
    update_args = conn.execute.await_args_list[0].args
    assert update_args[2] == "credit"
    assert update_args[6] == "credit"


@pytest.mark.asyncio
async def test_update_order_status_rejects_wallet_without_customer():
    tenant_id = uuid4()
    order_id = uuid4()
    conn = _pending_status_conn(
        order_id,
        datetime(2026, 6, 16, 14, 30),
        customer_id=None,
        group_row={"id": uuid4()},
    )

    with patch(
        "app.services.orders_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id, user_id=uuid4()),
    ), patch(
        "app.services.orders_service.get_db_connection",
        return_value=_AsyncContext(conn),
    ), patch(
        "app.services.orders_service.assert_order_not_in_closed_monthly_period",
        new=AsyncMock(),
    ):
        with pytest.raises(APIError) as exc:
            await orders_service.update_order_status(
                Request({"type": "http"}),
                order_id,
                "completed",
                "customer_wallet",
            )

    assert exc.value.status_code == 400
    assert exc.value.details["code"] == "customer_required"
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_order_gl_entry_uses_selected_method_account_before_slug_fallback():
    tenant_id = uuid4()
    order_id = uuid4()
    method_id = uuid4()
    debit_account_id = uuid4()
    ingresos_account_id = uuid4()
    entry_id = uuid4()

    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(
        side_effect=[
            {"code": "1120"},
            {"id": debit_account_id},
            {"id": ingresos_account_id},
            {"id": entry_id},
        ]
    )
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()
    conn.transaction = MagicMock(return_value=_AsyncContext())

    await cierre_service._post_order_gl_entry(
        conn=conn,
        tenant_id=tenant_id,
        order_id=order_id,
        order_date=date(2026, 6, 16),
        total_amount=Decimal("42000"),
        payment_method="cash",
        payment_method_id=method_id,
        tax_config={},
        order_number=8521,
    )

    method_lookup_args = conn.fetchrow.await_args_list[0].args
    debit_lookup_args = conn.fetchrow.await_args_list[1].args
    debit_line_args = conn.execute.await_args_list[0].args
    assert method_lookup_args[1] == method_id
    assert debit_lookup_args[1:] == (tenant_id, "1120")
    assert debit_line_args[2] == debit_account_id


@pytest.mark.asyncio
async def test_post_order_gl_entry_splits_table_advance_to_2810():
    tenant_id = uuid4()
    order_id = uuid4()
    advance_account_id = uuid4()
    ingresos_account_id = uuid4()
    entry_id = uuid4()

    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(
        side_effect=[
            {"id": advance_account_id},
            {"id": ingresos_account_id},
            {"id": entry_id},
        ]
    )
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()
    conn.transaction = MagicMock(return_value=_AsyncContext())

    await cierre_service._post_order_gl_entry(
        conn=conn,
        tenant_id=tenant_id,
        order_id=order_id,
        order_date=date(2026, 6, 16),
        total_amount=Decimal("70000"),
        payment_method="table_session_advance",
        payment_method_id=None,
        tax_config={},
        order_number=8522,
        advance_amount=Decimal("70000"),
    )

    advance_lookup_args = conn.fetchrow.await_args_list[0].args
    advance_line_args = conn.execute.await_args_list[0].args
    assert advance_lookup_args[1:] == (tenant_id, "2810")
    assert advance_line_args[2] == advance_account_id
    assert advance_line_args[3] == 70000.0
    assert "aplicación anticipo mesa" in advance_line_args[4]


def test_manual_order_modifier_quantity_defaults_to_one():
    modifier = ManualOrderModifier(id=str(uuid4()), name="Extra queso", price=2500)

    assert modifier.quantity == 1


@pytest.mark.asyncio
async def test_create_manual_order_captures_snapshots_and_posts_gl_cogs():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    order_item_id = uuid4()
    product_id = uuid4()
    payment_method_id = uuid4()
    order_date = datetime(2026, 6, 7, 10, 30)
    conn = _manual_order_conn(order_id, order_item_id, order_date)

    capture_snapshot = AsyncMock()
    deduct_modifier_inventory = AsyncMock()
    post_gl = AsyncMock()
    post_cogs = AsyncMock()

    with patch(
        "app.services.orders_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id, user_id=user_id),
    ), patch(
        "app.services.orders_service.get_db_connection",
        return_value=_AsyncContext(conn),
    ), patch(
        "app.services.orders_service.assert_order_not_in_closed_monthly_period",
        new=AsyncMock(),
    ), patch(
        "app.services.orders_service._pos_modifier_inventory_helpers",
        return_value=(deduct_modifier_inventory, None, None),
    ), patch(
        "app.services.orders_service._pos_order_item_ingredient_snapshot_helper",
        return_value=capture_snapshot,
    ), patch(
        "app.services.orders_service._get_tenant_tax_config",
        new=AsyncMock(return_value={"inc_enabled": True}),
    ), patch(
        "app.services.orders_service._post_order_gl_entry",
        new=post_gl,
    ), patch(
        "app.services.orders_service._post_order_cogs_gl_entry",
        new=post_cogs,
    ):
        result = await orders_service.create_manual_order(
            Request({"type": "http"}),
            order_date="2026-06-07T10:30:00",
            payment_method="digital",
            payment_method_id=str(payment_method_id),
            items=[
                {
                    "product_id": str(product_id),
                    "quantity": 2,
                    "unit_price": 15000,
                    "modifiers": [],
                }
            ],
        )

    assert result["success"] is True
    capture_snapshot.assert_awaited_once_with(
        conn,
        order_item_id,
        product_id,
        2.0,
        str(tenant_id),
    )
    post_gl.assert_awaited_once()
    assert post_gl.await_args.kwargs["order_id"] == order_id
    assert post_gl.await_args.kwargs["order_date"] == date(2026, 6, 7)
    assert post_gl.await_args.kwargs["payment_method_id"] == payment_method_id
    assert post_gl.await_args.kwargs["payment_method"] == "digital"
    post_cogs.assert_awaited_once_with(
        conn=conn,
        tenant_id=tenant_id,
        order_id=order_id,
        order_date=date(2026, 6, 7),
        order_number=14798,
    )


@pytest.mark.asyncio
async def test_create_manual_order_accounting_failures_do_not_block_sale():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    order_item_id = uuid4()
    product_id = uuid4()
    payment_method_id = uuid4()
    order_date = datetime(2026, 6, 7, 10, 30)
    conn = _manual_order_conn(order_id, order_item_id, order_date)

    with patch(
        "app.services.orders_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id, user_id=user_id),
    ), patch(
        "app.services.orders_service.get_db_connection",
        return_value=_AsyncContext(conn),
    ), patch(
        "app.services.orders_service.assert_order_not_in_closed_monthly_period",
        new=AsyncMock(),
    ), patch(
        "app.services.orders_service._pos_modifier_inventory_helpers",
        return_value=(AsyncMock(), None, None),
    ), patch(
        "app.services.orders_service._pos_order_item_ingredient_snapshot_helper",
        return_value=AsyncMock(),
    ), patch(
        "app.services.orders_service._get_tenant_tax_config",
        new=AsyncMock(return_value={"inc_enabled": True}),
    ), patch(
        "app.services.orders_service._post_order_gl_entry",
        new=AsyncMock(side_effect=RuntimeError("gl failed")),
    ) as post_gl, patch(
        "app.services.orders_service._post_order_cogs_gl_entry",
        new=AsyncMock(side_effect=RuntimeError("cogs failed")),
    ) as post_cogs:
        result = await orders_service.create_manual_order(
            Request({"type": "http"}),
            order_date="2026-06-07T10:30:00",
            payment_method="digital",
            payment_method_id=str(payment_method_id),
            items=[
                {
                    "product_id": str(product_id),
                    "quantity": 1,
                    "unit_price": 12000,
                    "modifiers": [],
                }
            ],
        )

    assert result["success"] is True
    assert result["data"]["id"] == str(order_id)
    post_gl.assert_awaited_once()
    post_cogs.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_manual_order_uses_modifier_quantity_for_totals_persistence_and_inventory():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    order_item_id = uuid4()
    product_id = uuid4()
    modifier_id = uuid4()
    payment_method_id = uuid4()
    order_date = datetime(2026, 6, 7, 10, 30)
    conn = _manual_order_conn(order_id, order_item_id, order_date)

    capture_snapshot = AsyncMock()
    deduct_modifier_inventory = AsyncMock()

    with patch(
        "app.services.orders_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id, user_id=user_id),
    ), patch(
        "app.services.orders_service.get_db_connection",
        return_value=_AsyncContext(conn),
    ), patch(
        "app.services.orders_service.assert_order_not_in_closed_monthly_period",
        new=AsyncMock(),
    ), patch(
        "app.services.orders_service._pos_modifier_inventory_helpers",
        return_value=(deduct_modifier_inventory, None, None),
    ), patch(
        "app.services.orders_service._pos_order_item_ingredient_snapshot_helper",
        return_value=capture_snapshot,
    ), patch(
        "app.services.orders_service._get_tenant_tax_config",
        new=AsyncMock(return_value={"inc_enabled": True}),
    ), patch(
        "app.services.orders_service._post_order_gl_entry",
        new=AsyncMock(),
    ), patch(
        "app.services.orders_service._post_order_cogs_gl_entry",
        new=AsyncMock(),
    ):
        result = await orders_service.create_manual_order(
            Request({"type": "http"}),
            order_date="2026-06-07T10:30:00",
            payment_method="digital",
            payment_method_id=str(payment_method_id),
            items=[
                {
                    "product_id": str(product_id),
                    "quantity": 2,
                    "unit_price": 15000,
                    "modifiers": [
                        {
                            "id": str(modifier_id),
                            "name": "Extra queso",
                            "price": 2500,
                            "quantity": 2,
                        }
                    ],
                }
            ],
        )

    assert result["success"] is True
    order_insert_args = conn.fetchrow.await_args_list[0].args
    item_insert_args = conn.fetchrow.await_args_list[1].args
    modifier_insert_args = conn.execute.await_args_list[0].args

    assert order_insert_args[6] == 40000
    assert item_insert_args[5] == 40000
    assert modifier_insert_args[5] == 2.0
    deduct_modifier_inventory.assert_awaited_once()
    assert deduct_modifier_inventory.await_args.kwargs["modifier_qty"] == 2.0
    assert deduct_modifier_inventory.await_args.kwargs["modifier"]["quantity"] == 2


@pytest.mark.asyncio
async def test_create_manual_order_legacy_modifier_payload_defaults_quantity_to_one():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    order_item_id = uuid4()
    product_id = uuid4()
    modifier_id = uuid4()
    payment_method_id = uuid4()
    order_date = datetime(2026, 6, 7, 10, 30)
    conn = _manual_order_conn(order_id, order_item_id, order_date)

    deduct_modifier_inventory = AsyncMock()

    with patch(
        "app.services.orders_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id, user_id=user_id),
    ), patch(
        "app.services.orders_service.get_db_connection",
        return_value=_AsyncContext(conn),
    ), patch(
        "app.services.orders_service.assert_order_not_in_closed_monthly_period",
        new=AsyncMock(),
    ), patch(
        "app.services.orders_service._pos_modifier_inventory_helpers",
        return_value=(deduct_modifier_inventory, None, None),
    ), patch(
        "app.services.orders_service._pos_order_item_ingredient_snapshot_helper",
        return_value=AsyncMock(),
    ), patch(
        "app.services.orders_service._get_tenant_tax_config",
        new=AsyncMock(return_value={"inc_enabled": True}),
    ), patch(
        "app.services.orders_service._post_order_gl_entry",
        new=AsyncMock(),
    ), patch(
        "app.services.orders_service._post_order_cogs_gl_entry",
        new=AsyncMock(),
    ):
        result = await orders_service.create_manual_order(
            Request({"type": "http"}),
            order_date="2026-06-07T10:30:00",
            payment_method="digital",
            payment_method_id=str(payment_method_id),
            items=[
                {
                    "product_id": str(product_id),
                    "quantity": 2,
                    "unit_price": 15000,
                    "modifiers": [
                        {
                            "id": str(modifier_id),
                            "name": "Extra queso",
                            "price": 2500,
                        }
                    ],
                }
            ],
        )

    assert result["success"] is True
    order_insert_args = conn.fetchrow.await_args_list[0].args
    item_insert_args = conn.fetchrow.await_args_list[1].args
    modifier_insert_args = conn.execute.await_args_list[0].args

    assert order_insert_args[6] == 35000
    assert item_insert_args[5] == 35000
    assert modifier_insert_args[5] == 1.0
    assert deduct_modifier_inventory.await_args.kwargs["modifier_qty"] == 1.0

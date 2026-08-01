from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import Request

from app.core.exceptions import APIError
from app.routers.orders import ManualOrderModifier
from app.services import cierre_service, orders_service
from app.services.account_role_service import AccountRef, AccountRole


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
    assert conn.fetchval.await_count == 2
    assert "tenant_public_profiles" in conn.fetchval.await_args_list[0].args[0]
    assert "tenant_ingredient_movements" in conn.fetchval.await_args_list[1].args[0]
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
    assert conn.fetchval.await_count == 2
    assert "tenant_public_profiles" in conn.fetchval.await_args_list[0].args[0]
    assert "tenant_ingredient_movements" in conn.fetchval.await_args_list[1].args[0]
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

    async def fetchrow_side_effect(query, *args):
        if "FROM payment_methods pm" in query:
            return {"code": "1120"}
        if "FROM tenant_accounts" in query and len(args) >= 2 and args[1] == "1120":
            return {"id": debit_account_id}
        if "FROM tenant_accounts" in query and len(args) >= 2 and args[1] == "4175":
            return {"id": ingresos_account_id}
        if "INSERT INTO tenant_journal_entries" in query:
            return {"id": entry_id}
        return None

    conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effect)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()
    conn.transaction = MagicMock(return_value=_AsyncContext())

    payment_resolver = AsyncMock(return_value=AccountRef(
        debit_account_id, "BANK-CARD", "Selected bank", AccountRole.BANK, "explicit_account_id"
    ))
    role_resolver = AsyncMock(return_value=AccountRef(
        ingresos_account_id, "REVENUE", "Revenue", AccountRole.SALES_REVENUE, "localization_default"
    ))
    with patch("app.services.cierre_service.resolve_payment_account", new=payment_resolver), patch(
        "app.services.cierre_service.resolve_account", new=role_resolver
    ):
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

    debit_line_args = conn.execute.await_args_list[0].args
    assert payment_resolver.await_args.kwargs["payment_method_id"] == method_id
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

    async def fetchrow_side_effect(query, *args):
        if "FROM tenant_accounts" in query and len(args) >= 2 and args[1] == "2810":
            return {"id": advance_account_id}
        if "FROM tenant_accounts" in query and len(args) >= 2 and args[1] == "4175":
            return {"id": ingresos_account_id}
        if "INSERT INTO tenant_journal_entries" in query:
            return {"id": entry_id}
        return None

    conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effect)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()
    conn.transaction = MagicMock(return_value=_AsyncContext())

    advance_ref = AccountRef(
        advance_account_id, "ADVANCE", "Customer advances", AccountRole.CUSTOMER_ADVANCES, "localization_default"
    )
    revenue_ref = AccountRef(
        ingresos_account_id, "REVENUE", "Revenue", AccountRole.SALES_REVENUE, "localization_default"
    )
    async def resolve_role(_conn, _tenant_id, role, **_kwargs):
        return advance_ref if role == AccountRole.CUSTOMER_ADVANCES else revenue_ref

    with patch("app.services.cierre_service.resolve_payment_account", new=AsyncMock(return_value=advance_ref)), patch(
        "app.services.cierre_service.resolve_account", new=AsyncMock(side_effect=resolve_role)
    ):
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

    advance_line_args = conn.execute.await_args_list[0].args
    assert advance_line_args[2] == advance_account_id
    assert advance_line_args[3] == 70000.0
    assert "aplicación anticipo mesa" in advance_line_args[4]


@pytest.mark.asyncio
async def test_post_order_gl_entry_splits_debits_by_payment_puc():
    tenant_id = uuid4()
    order_id = uuid4()
    digital_method_id = uuid4()
    card_method_id = uuid4()
    digital_account_id = uuid4()
    card_account_id = uuid4()
    ingresos_account_id = uuid4()
    entry_id = uuid4()

    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)

    async def fetchrow_side_effect(query, *args):
        if "FROM payment_methods pm" in query and args[0] == digital_method_id:
            return {"code": "111025"}
        if "FROM payment_methods pm" in query and args[0] == card_method_id:
            return {"code": "111040"}
        if "FROM tenant_accounts" in query and len(args) >= 2 and args[1] == "111025":
            return {"id": digital_account_id}
        if "FROM tenant_accounts" in query and len(args) >= 2 and args[1] == "111040":
            return {"id": card_account_id}
        if "FROM tenant_accounts" in query and len(args) >= 2 and args[1] == "4175":
            return {"id": ingresos_account_id}
        if "INSERT INTO tenant_journal_entries" in query:
            return {"id": entry_id}
        return None

    conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effect)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()
    conn.transaction = MagicMock(return_value=_AsyncContext())

    async def resolve_payment(_conn, _tenant_id, _slug, payment_method_id=None, **_kwargs):
        account_id = digital_account_id if payment_method_id == digital_method_id else card_account_id
        return AccountRef(account_id, "BANK", "Bank", AccountRole.BANK, "explicit_account_id")

    with patch("app.services.cierre_service.resolve_payment_account", new=AsyncMock(side_effect=resolve_payment)), patch(
        "app.services.cierre_service.resolve_account",
        new=AsyncMock(return_value=AccountRef(ingresos_account_id, "REVENUE", "Revenue", AccountRole.SALES_REVENUE, "localization_default")),
    ):
        await cierre_service._post_order_gl_entry(
            conn=conn,
            tenant_id=tenant_id,
            order_id=order_id,
            order_date=date(2026, 6, 19),
            total_amount=Decimal("45000"),
            payment_method="card",
            payment_method_id=card_method_id,
            tax_config={},
            order_number=15342,
            payment_splits=[
                {"amount": Decimal("42000"), "payment_method": "digital", "payment_method_id": digital_method_id},
                {"amount": Decimal("3000"), "payment_method": "card", "payment_method_id": card_method_id},
            ],
        )

    debit_lines = [
        call.args for call in conn.execute.await_args_list
        if "INSERT INTO tenant_journal_lines" in call.args[0]
        and call.args[2] in (digital_account_id, card_account_id)
    ]
    assert debit_lines[0][2] == digital_account_id
    assert debit_lines[0][3] == 42000.0
    assert debit_lines[1][2] == card_account_id
    assert debit_lines[1][3] == 3000.0


@pytest.mark.asyncio
async def test_post_order_gl_entry_keeps_exact_split_amounts_with_tip():
    """Tip must not prorate tender debits (e.g. 10000 cash stays 10000, not 7142.86)."""
    tenant_id = uuid4()
    order_id = uuid4()
    cash_method_id = uuid4()
    credit_method_id = uuid4()
    wallet_method_id = uuid4()
    digital_method_id = uuid4()
    cash_account_id = uuid4()
    credit_account_id = uuid4()
    wallet_account_id = uuid4()
    digital_account_id = uuid4()
    ingresos_account_id = uuid4()
    entry_id = uuid4()

    method_to_account = {
        cash_method_id: cash_account_id,
        credit_method_id: credit_account_id,
        wallet_method_id: wallet_account_id,
        digital_method_id: digital_account_id,
    }

    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)

    async def fetchrow_side_effect(query, *args):
        if "INSERT INTO tenant_journal_entries" in query:
            return {"id": entry_id}
        return None

    conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effect)
    # No line items → product_gross falls back to total_amount (43000)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()
    conn.transaction = MagicMock(return_value=_AsyncContext())

    async def resolve_payment(_conn, _tenant_id, _slug, payment_method_id=None, **_kwargs):
        return AccountRef(
            method_to_account[payment_method_id],
            "PAY",
            "Pay",
            AccountRole.CASH,
            "explicit_account_id",
        )

    with patch(
        "app.services.cierre_service.resolve_payment_account",
        new=AsyncMock(side_effect=resolve_payment),
    ), patch(
        "app.services.cierre_service.resolve_account",
        new=AsyncMock(
            return_value=AccountRef(
                ingresos_account_id, "REVENUE", "Revenue", AccountRole.SALES_REVENUE, "localization_default"
            )
        ),
    ):
        await cierre_service._post_order_gl_entry(
            conn=conn,
            tenant_id=tenant_id,
            order_id=order_id,
            order_date=date(2026, 8, 1),
            total_amount=Decimal("43000"),
            payment_method="digital",
            payment_method_id=digital_method_id,
            tax_config={},
            order_number=17441,
            tip_amount=Decimal("17200"),
            tip_tax_amount=Decimal("0"),
            payment_splits=[
                {"amount": Decimal("10000"), "payment_method": "cash", "payment_method_id": cash_method_id},
                {"amount": Decimal("5000"), "payment_method": "credit", "payment_method_id": credit_method_id},
                {"amount": Decimal("20000"), "payment_method": "customer_wallet", "payment_method_id": wallet_method_id},
                {"amount": Decimal("25200"), "payment_method": "digital", "payment_method_id": digital_method_id},
            ],
        )

    debit_by_account = {
        call.args[2]: call.args[3]
        for call in conn.execute.await_args_list
        if "INSERT INTO tenant_journal_lines" in call.args[0] and call.args[3] and float(call.args[3]) > 0
    }
    assert debit_by_account[cash_account_id] == 10000.0
    assert debit_by_account[credit_account_id] == 5000.0
    assert debit_by_account[wallet_account_id] == 20000.0
    assert debit_by_account[digital_account_id] == 25200.0

    entry_insert = next(
        call.args for call in conn.fetchrow.await_args_list
        if call.args and "INSERT INTO tenant_journal_entries" in call.args[0]
    )
    assert entry_insert[7] == 60200.0  # total_debit
    assert entry_insert[8] == 60200.0  # total_credit


@pytest.mark.asyncio
async def test_post_order_gl_entry_single_payment_includes_exact_tip_debit():
    tenant_id = uuid4()
    order_id = uuid4()
    cash_method_id = uuid4()
    cash_account_id = uuid4()
    ingresos_account_id = uuid4()
    entry_id = uuid4()

    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)

    async def fetchrow_side_effect(query, *args):
        if "INSERT INTO tenant_journal_entries" in query:
            return {"id": entry_id}
        return None

    conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effect)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()
    conn.transaction = MagicMock(return_value=_AsyncContext())

    with patch(
        "app.services.cierre_service.resolve_payment_account",
        new=AsyncMock(
            return_value=AccountRef(cash_account_id, "CASH", "Cash", AccountRole.CASH, "explicit_account_id")
        ),
    ), patch(
        "app.services.cierre_service.resolve_account",
        new=AsyncMock(
            return_value=AccountRef(
                ingresos_account_id, "REVENUE", "Revenue", AccountRole.SALES_REVENUE, "localization_default"
            )
        ),
    ):
        await cierre_service._post_order_gl_entry(
            conn=conn,
            tenant_id=tenant_id,
            order_id=order_id,
            order_date=date(2026, 8, 1),
            total_amount=Decimal("43000"),
            payment_method="cash",
            payment_method_id=cash_method_id,
            tax_config={},
            order_number=1,
            tip_amount=Decimal("17200"),
            tip_tax_amount=Decimal("0"),
        )

    cash_debits = [
        call.args[3]
        for call in conn.execute.await_args_list
        if "INSERT INTO tenant_journal_lines" in call.args[0] and call.args[2] == cash_account_id
    ]
    assert cash_debits == [60200.0]


@pytest.mark.asyncio
async def test_post_order_gl_entry_tip_from_tender_excess_balances_lines():
    """Mesa-style: tip not passed; each order credits only tender excess beyond product."""
    tenant_id = uuid4()
    order_id = uuid4()
    cash_method_id = uuid4()
    cash_account_id = uuid4()
    ingresos_account_id = uuid4()
    entry_id = uuid4()

    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)

    async def fetchrow_side_effect(query, *args):
        if "INSERT INTO tenant_journal_entries" in query:
            return {"id": entry_id}
        return None

    conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effect)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()
    conn.transaction = MagicMock(return_value=_AsyncContext())

    with patch(
        "app.services.cierre_service.resolve_payment_account",
        new=AsyncMock(
            return_value=AccountRef(cash_account_id, "CASH", "Cash", AccountRole.CASH, "explicit_account_id")
        ),
    ), patch(
        "app.services.cierre_service.resolve_account",
        new=AsyncMock(
            return_value=AccountRef(
                ingresos_account_id, "REVENUE", "Revenue", AccountRole.SALES_REVENUE, "localization_default"
            )
        ),
    ):
        await cierre_service._post_order_gl_entry(
            conn=conn,
            tenant_id=tenant_id,
            order_id=order_id,
            order_date=date(2026, 8, 1),
            total_amount=Decimal("20000"),
            payment_method="cash",
            payment_method_id=cash_method_id,
            tax_config={},
            order_number=2,
            tip_amount=Decimal("0"),
            payment_splits=[
                {"amount": Decimal("25000"), "payment_method": "cash", "payment_method_id": cash_method_id},
            ],
        )

    debits = credits = 0.0
    for call in conn.execute.await_args_list:
        q = call.args[0]
        if "INSERT INTO tenant_journal_lines" not in q:
            continue
        if "VALUES ($1, $2, $3, 0, $4, $5)" in q:
            debits += float(call.args[3])
        elif "VALUES ($1, $2, 0, $3, $4, $5)" in q:
            credits += float(call.args[3])
    assert debits == 25000.0
    assert credits == 25000.0


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
    order_date = datetime(2026, 6, 7, 0, 30, tzinfo=timezone.utc)
    conn = _manual_order_conn(order_id, order_item_id, order_date)
    conn.fetchval = AsyncMock(return_value="Europe/Madrid")

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
            order_date="2026-06-07T00:30:00+00:00",
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
    order_insert = next(
        call.args
        for call in conn.fetchrow.await_args_list
        if "INSERT INTO orders" in call.args[0]
    )
    assert order_insert[7] == "paid"
    assert result["data"]["payment_status"] == "paid"
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
async def test_create_manual_credit_persists_credit_status_for_identified_customer():
    tenant_id = uuid4()
    user_id = uuid4()
    customer_id = uuid4()
    order_id = uuid4()
    order_item_id = uuid4()
    product_id = uuid4()
    order_date = datetime(2026, 7, 13, 18, 42, tzinfo=timezone.utc)

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {"phone_number": "3001234567"},
            {
                "id": order_id,
                "order_number": 16484,
                "order_date": order_date,
                "created_at": order_date,
            },
            {"id": order_item_id},
        ]
    )
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value="America/Bogota")
    conn.execute = AsyncMock()
    conn.transaction = MagicMock(return_value=_AsyncContext())

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
        new=AsyncMock(),
    ), patch(
        "app.services.orders_service._post_order_cogs_gl_entry",
        new=AsyncMock(),
    ):
        result = await orders_service.create_manual_order(
            Request({"type": "http"}),
            order_date="2026-07-13T18:42:00+00:00",
            payment_method="credit",
            customer_id=str(customer_id),
            items=[
                {
                    "product_id": str(product_id),
                    "quantity": 1,
                    "unit_price": 270000,
                    "modifiers": [],
                }
            ],
        )

    order_insert = next(
        call.args
        for call in conn.fetchrow.await_args_list
        if "INSERT INTO orders" in call.args[0]
    )
    assert order_insert[7] == "credit"
    assert result["data"]["payment_status"] == "credit"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payment_method", "payments"),
    [
        ("credit", None),
        (
            "cash",
            [
                {
                    "amount": 1000,
                    "payment_method": "credit",
                    "payment_method_id": None,
                }
            ],
        ),
    ],
)
async def test_create_manual_credit_tender_requires_customer(
    payment_method,
    payments,
):
    tenant_id = uuid4()
    get_connection = MagicMock()

    with patch(
        "app.services.orders_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id, user_id=uuid4()),
    ), patch(
        "app.services.orders_service.get_db_connection",
        get_connection,
    ):
        with pytest.raises(APIError) as exc:
            await orders_service.create_manual_order(
                Request({"type": "http"}),
                order_date="2026-07-13T18:42:00+00:00",
                payment_method=payment_method,
                payments=payments,
                items=[
                    {
                        "product_id": str(uuid4()),
                        "quantity": 1,
                        "unit_price": 1000,
                        "modifiers": [],
                    }
                ],
            )

    assert exc.value.status_code == 400
    assert "cliente identificado" in str(exc.value)
    get_connection.assert_not_called()


@pytest.mark.asyncio
async def test_create_manual_credit_rejects_anonymous_customer():
    tenant_id = uuid4()
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value="America/Bogota")
    conn.fetchrow = AsyncMock(return_value={"phone_number": "0000000000"})
    conn.transaction = MagicMock(return_value=_AsyncContext())

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
            await orders_service.create_manual_order(
                Request({"type": "http"}),
                order_date="2026-07-13T18:42:00+00:00",
                payment_method="credit",
                customer_id=str(uuid4()),
                items=[
                    {
                        "product_id": str(uuid4()),
                        "quantity": 1,
                        "unit_price": 1000,
                        "modifiers": [],
                    }
                ],
            )

    assert exc.value.status_code == 400
    assert "no anónimo" in str(exc.value)


@pytest.mark.asyncio
async def test_create_manual_order_persists_split_payments_for_gl():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    order_item_id = uuid4()
    first_payment_id = uuid4()
    second_payment_id = uuid4()
    product_id = uuid4()
    digital_method_id = uuid4()
    card_method_id = uuid4()
    order_date = datetime(2026, 6, 7, 10, 30)

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
            {"id": first_payment_id},
            {"id": second_payment_id},
        ]
    )
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()
    conn.transaction = MagicMock(return_value=_AsyncContext())

    post_gl = AsyncMock()

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
        new=post_gl,
    ), patch(
        "app.services.orders_service._post_order_cogs_gl_entry",
        new=AsyncMock(),
    ):
        result = await orders_service.create_manual_order(
            Request({"type": "http"}),
            order_date="2026-06-07T10:30:00",
            payment_method="digital",
            payment_method_id=str(digital_method_id),
            items=[
                {
                    "product_id": str(product_id),
                    "quantity": 1,
                    "unit_price": 45000,
                    "modifiers": [],
                }
            ],
            payments=[
                {
                    "amount": 42000,
                    "payment_method": "digital",
                    "payment_method_id": str(digital_method_id),
                },
                {
                    "amount": 3000,
                    "payment_method": "card",
                    "payment_method_id": str(card_method_id),
                },
            ],
        )

    payment_inserts = [
        call.args for call in conn.fetchrow.await_args_list
        if "INSERT INTO order_payments" in call.args[0]
    ]
    assert result["success"] is True
    order_insert = next(
        call.args
        for call in conn.fetchrow.await_args_list
        if "INSERT INTO orders" in call.args[0]
    )
    assert order_insert[7] == "paid"
    assert result["data"]["payment_status"] == "paid"
    assert len(payment_inserts) == 2
    assert payment_inserts[0][3] == 42000
    assert payment_inserts[0][4] == "digital"
    assert payment_inserts[0][5] == str(digital_method_id)
    assert payment_inserts[1][3] == 3000
    assert payment_inserts[1][4] == "card"
    assert payment_inserts[1][5] == str(card_method_id)
    assert post_gl.await_args.kwargs["payment_splits"][0]["amount"] == 42000
    assert post_gl.await_args.kwargs["payment_splits"][1]["payment_method"] == "card"


def test_manual_order_payment_status_backfill_is_scoped_and_non_destructive():
    sql = Path("migrations/102_backfill_manual_order_payment_status.sql").read_text()
    normalized = " ".join(sql.lower().split())

    assert "set payment_status = 'credit'" in normalized
    assert "set payment_status = 'paid'" in normalized
    assert normalized.count("payment_status is null") == 2
    assert "extra_attributes->>'source' = 'manual'" in normalized
    assert "o.status = 'completed'" in normalized
    assert "o.credit_paid_amount = 0" in normalized
    assert "op.voided_at is null" in normalized
    assert "select sum(op.amount)" in normalized
    assert "insert into order_payments" not in normalized
    assert "update credit_payments" not in normalized
    assert "tenant_journal_entries" not in normalized


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
    ), patch(
        "app.services.orders_service.resolve_modifier_selections",
        new=AsyncMock(return_value=[{
            "id": modifier_id,
            "name": "Extra queso",
            "price": Decimal("2500"),
            "quantity": 2,
            "included_quantity": 0,
            "chargeable_quantity": 2,
            "subtotal": Decimal("5000"),
        }]),
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
    ), patch(
        "app.services.orders_service.resolve_modifier_selections",
        new=AsyncMock(return_value=[{
            "id": modifier_id,
            "name": "Extra queso",
            "price": Decimal("2500"),
            "quantity": 1,
            "included_quantity": 0,
            "chargeable_quantity": 1,
            "subtotal": Decimal("2500"),
        }]),
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

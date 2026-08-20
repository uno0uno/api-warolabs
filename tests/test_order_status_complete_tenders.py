"""Pending → completed PATCH accepts cash received, credit due date, and waiter."""
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import Request

from app.core.exceptions import APIError
from app.services import orders_service


class _AsyncContext:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _session(tenant_id, user_id):
    return SimpleNamespace(tenant_id=tenant_id, user_id=user_id)


def _pending_row(order_id, *, customer_id=None, total_amount=20000):
    return {
        "id": order_id,
        "status": "pending",
        "order_number": 23631,
        "table_session_id": None,
        "pos_cart_id": uuid4(),
        "payment_status": None,
        "order_date": datetime(2026, 8, 19, 12, 0),
        "total_amount": total_amount,
        "customer_id": customer_id,
        "discount_amount": 0,
    }


def _complete_patches(conn, tenant_id, user_id):
    return (
        patch("app.services.orders_service.require_valid_session", return_value=_session(tenant_id, user_id)),
        patch("app.services.orders_service.get_db_connection", return_value=_AsyncContext(conn)),
        patch("app.services.orders_service.resolve_tenant_timezone", new=AsyncMock(return_value="America/Bogota")),
        patch("app.services.orders_service.assert_order_not_in_closed_monthly_period", new=AsyncMock()),
        patch("app.services.orders_service.assert_order_invoice_allows_mutation", new=AsyncMock()),
        patch("app.services.orders_service._order_inventory_already_consumed_before_completion", new=AsyncMock(return_value=True)),
        patch("app.services.orders_service._get_tenant_tax_config", new=AsyncMock(return_value={"inc_enabled": True})),
        patch("app.services.orders_service._post_order_gl_entry", new=AsyncMock()),
        patch("app.services.orders_service._post_order_cogs_gl_entry", new=AsyncMock()),
        patch("app.services.orders_service.evaluate_and_award", new=MagicMock(return_value=object())),
        patch("app.services.orders_service.asyncio.create_task", new=MagicMock()),
        patch("app.services.orders_service.record_operation_event", new=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_complete_credit_without_customer_is_400():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=_pending_row(order_id))
    conn.execute = AsyncMock()

    with patch("app.services.orders_service.require_valid_session", return_value=_session(tenant_id, user_id)), \
         patch("app.services.orders_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.orders_service.resolve_tenant_timezone", new=AsyncMock(return_value="America/Bogota")), \
         patch("app.services.orders_service.assert_order_not_in_closed_monthly_period", new=AsyncMock()), \
         patch("app.services.orders_service.assert_order_invoice_allows_mutation", new=AsyncMock()):
        with pytest.raises(APIError) as exc:
            await orders_service.update_order_status(
                Request({"type": "http"}),
                order_id,
                "completed",
                "credit",
            )

    assert exc.value.status_code == 400
    assert exc.value.details.get("code") == "customer_required"
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_complete_cash_received_below_total_is_400():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=_pending_row(order_id, total_amount=20000))
    conn.execute = AsyncMock()

    with patch("app.services.orders_service.require_valid_session", return_value=_session(tenant_id, user_id)), \
         patch("app.services.orders_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.orders_service.resolve_tenant_timezone", new=AsyncMock(return_value="America/Bogota")), \
         patch("app.services.orders_service.assert_order_not_in_closed_monthly_period", new=AsyncMock()), \
         patch("app.services.orders_service.assert_order_invoice_allows_mutation", new=AsyncMock()):
        with pytest.raises(APIError) as exc:
            await orders_service.update_order_status(
                Request({"type": "http"}),
                order_id,
                "completed",
                "cash",
                cash_received=15000,
            )

    assert exc.value.status_code == 400
    assert exc.value.details.get("code") == "cash_received_short"
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_complete_card_rejects_cash_received():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=_pending_row(order_id))
    conn.execute = AsyncMock()

    with patch("app.services.orders_service.require_valid_session", return_value=_session(tenant_id, user_id)), \
         patch("app.services.orders_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.orders_service.resolve_tenant_timezone", new=AsyncMock(return_value="America/Bogota")), \
         patch("app.services.orders_service.assert_order_not_in_closed_monthly_period", new=AsyncMock()), \
         patch("app.services.orders_service.assert_order_invoice_allows_mutation", new=AsyncMock()):
        with pytest.raises(APIError) as exc:
            await orders_service.update_order_status(
                Request({"type": "http"}),
                order_id,
                "completed",
                "card",
                cash_received=20000,
            )

    assert exc.value.status_code == 400
    assert "efectivo" in exc.value.message
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_complete_cash_persists_cash_received_and_waiter():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    waiter_id = uuid4()
    order_date = datetime(2026, 8, 19, 12, 0)
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            _pending_row(order_id, total_amount=20000),
            {"id": uuid4()},
            {
                "id": order_id,
                "order_number": 23631,
                "total_amount": 20000,
                "payment_method": "cash",
                "payment_method_id": None,
                "order_date": order_date,
                "tip_amount": 0,
                "tip_tax_amount": 0,
            },
        ]
    )
    conn.fetchval = AsyncMock(return_value=waiter_id)
    conn.execute = AsyncMock()

    patches = _complete_patches(conn, tenant_id, user_id)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
         patches[6], patches[7], patches[8], patches[9], patches[10], patches[11]:
        result = await orders_service.update_order_status(
            Request({"type": "http"}),
            order_id,
            "completed",
            "cash",
            cash_received=25000,
            served_by_member_id=waiter_id,
        )

    assert result["success"] is True
    update_args = conn.execute.await_args_list[0].args
    assert update_args[2] == "cash"
    assert update_args[7] == Decimal("25000")
    assert update_args[9] == waiter_id


@pytest.mark.asyncio
async def test_complete_credit_persists_due_date():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    customer_id = uuid4()
    order_date = datetime(2026, 8, 19, 12, 0)
    due = date(2026, 9, 1)
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            _pending_row(order_id, customer_id=customer_id, total_amount=45000),
            {"id": uuid4()},
            {
                "id": order_id,
                "order_number": 23631,
                "total_amount": 45000,
                "payment_method": "credit",
                "payment_method_id": None,
                "order_date": order_date,
                "tip_amount": 0,
                "tip_tax_amount": 0,
            },
        ]
    )
    conn.execute = AsyncMock()

    patches = _complete_patches(conn, tenant_id, user_id)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
         patches[6], patches[7], patches[8], patches[9], patches[10], patches[11]:
        result = await orders_service.update_order_status(
            Request({"type": "http"}),
            order_id,
            "completed",
            "credit",
            customer_id=str(customer_id),
            credit_due_date=due,
        )

    assert result["success"] is True
    update_args = conn.execute.await_args_list[0].args
    assert update_args[2] == "credit"
    assert update_args[6] == "credit"
    assert update_args[8] == due
    assert update_args[10] is None


def test_complete_manual_discount_percent_and_fixed():
    assert orders_service._complete_manual_discount(
        current_total=20000, current_discount_amount=0,
        discount_type="percent", discount_value=10,
    ) == ("percent", 10.0, 2000.0, 18000.0)
    assert orders_service._complete_manual_discount(
        current_total=20000, current_discount_amount=0,
        discount_type="fixed", discount_value=5000,
    ) == ("fixed", 5000.0, 5000.0, 15000.0)
    assert orders_service._complete_manual_discount(
        current_total=18000, current_discount_amount=2000,
        discount_type="fixed", discount_value=5000,
    ) == ("fixed", 5000.0, 5000.0, 15000.0)
    assert orders_service._complete_manual_discount(
        current_total=20000, current_discount_amount=0,
        discount_type="percent", discount_value=0,
    ) == (None, None, None, None)


@pytest.mark.asyncio
async def test_complete_percent_discount_persists_and_lowers_total():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    order_date = datetime(2026, 8, 19, 12, 0)
    gl_mock = AsyncMock()
    cogs_mock = AsyncMock()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            _pending_row(order_id, total_amount=20000),
            {"id": uuid4()},
            {
                "id": order_id,
                "order_number": 23631,
                "total_amount": 18000,
                "payment_method": "card",
                "payment_method_id": None,
                "order_date": order_date,
                "tip_amount": 0,
                "tip_tax_amount": 0,
            },
        ]
    )
    conn.execute = AsyncMock()

    patches = (
        patch("app.services.orders_service.require_valid_session", return_value=_session(tenant_id, user_id)),
        patch("app.services.orders_service.get_db_connection", return_value=_AsyncContext(conn)),
        patch("app.services.orders_service.resolve_tenant_timezone", new=AsyncMock(return_value="America/Bogota")),
        patch("app.services.orders_service.assert_order_not_in_closed_monthly_period", new=AsyncMock()),
        patch("app.services.orders_service.assert_order_invoice_allows_mutation", new=AsyncMock()),
        patch("app.services.orders_service._order_inventory_already_consumed_before_completion", new=AsyncMock(return_value=True)),
        patch("app.services.orders_service._get_tenant_tax_config", new=AsyncMock(return_value={"inc_enabled": True})),
        patch("app.services.orders_service._post_order_gl_entry", new=gl_mock),
        patch("app.services.orders_service._post_order_cogs_gl_entry", new=cogs_mock),
        patch("app.services.orders_service.evaluate_and_award", new=MagicMock(return_value=object())),
        patch("app.services.orders_service.asyncio.create_task", new=MagicMock()),
        patch("app.services.orders_service.record_operation_event", new=AsyncMock()),
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
         patches[6], patches[7], patches[8], patches[9], patches[10], patches[11]:
        result = await orders_service.update_order_status(
            Request({"type": "http"}),
            order_id,
            "completed",
            "card",
            discount_type="percent",
            discount_value=10,
        )

    assert result["success"] is True
    update_args = conn.execute.await_args_list[0].args
    assert update_args[10] == "percent"
    assert update_args[11] == 10.0
    assert update_args[12] == 2000.0
    assert update_args[13] == 18000.0
    assert gl_mock.await_args.kwargs["total_amount"] == Decimal("18000")
    cogs_mock.assert_awaited()


@pytest.mark.asyncio
async def test_complete_fixed_discount_persists():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    order_date = datetime(2026, 8, 19, 12, 0)
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            _pending_row(order_id, total_amount=20000),
            {"id": uuid4()},
            {
                "id": order_id,
                "order_number": 23631,
                "total_amount": 15000,
                "payment_method": "card",
                "payment_method_id": None,
                "order_date": order_date,
                "tip_amount": 0,
                "tip_tax_amount": 0,
            },
        ]
    )
    conn.execute = AsyncMock()

    patches = _complete_patches(conn, tenant_id, user_id)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
         patches[6], patches[7], patches[8], patches[9], patches[10], patches[11]:
        result = await orders_service.update_order_status(
            Request({"type": "http"}),
            order_id,
            "completed",
            "card",
            discount_type="fixed",
            discount_value=5000,
        )

    assert result["success"] is True
    update_args = conn.execute.await_args_list[0].args
    assert update_args[10] == "fixed"
    assert update_args[12] == 5000.0
    assert update_args[13] == 15000.0


@pytest.mark.asyncio
async def test_complete_omit_and_zero_discount_leave_total_unchanged():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    order_date = datetime(2026, 8, 19, 12, 0)
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            _pending_row(order_id, total_amount=20000),
            {"id": uuid4()},
            {
                "id": order_id,
                "order_number": 23631,
                "total_amount": 20000,
                "payment_method": "card",
                "payment_method_id": None,
                "order_date": order_date,
                "tip_amount": 0,
                "tip_tax_amount": 0,
            },
            _pending_row(order_id, total_amount=20000),
            {"id": uuid4()},
            {
                "id": order_id,
                "order_number": 23631,
                "total_amount": 20000,
                "payment_method": "card",
                "payment_method_id": None,
                "order_date": order_date,
                "tip_amount": 0,
                "tip_tax_amount": 0,
            },
        ]
    )
    conn.execute = AsyncMock()

    patches = _complete_patches(conn, tenant_id, user_id)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
         patches[6], patches[7], patches[8], patches[9], patches[10], patches[11]:
        await orders_service.update_order_status(
            Request({"type": "http"}),
            order_id,
            "completed",
            "card",
        )
        await orders_service.update_order_status(
            Request({"type": "http"}),
            order_id,
            "completed",
            "card",
            discount_type="percent",
            discount_value=0,
        )

    omit_args = conn.execute.await_args_list[0].args
    zero_args = conn.execute.await_args_list[1].args
    assert omit_args[10] is None
    assert omit_args[13] is None
    assert zero_args[10] is None
    assert zero_args[13] is None


@pytest.mark.asyncio
async def test_complete_invalid_discount_is_400():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=_pending_row(order_id, total_amount=20000))
    conn.execute = AsyncMock()

    async def _reject(**kwargs):
        with patch("app.services.orders_service.require_valid_session", return_value=_session(tenant_id, user_id)), \
             patch("app.services.orders_service.get_db_connection", return_value=_AsyncContext(conn)), \
             patch("app.services.orders_service.resolve_tenant_timezone", new=AsyncMock(return_value="America/Bogota")), \
             patch("app.services.orders_service.assert_order_not_in_closed_monthly_period", new=AsyncMock()), \
             patch("app.services.orders_service.assert_order_invoice_allows_mutation", new=AsyncMock()):
            with pytest.raises(APIError) as exc:
                await orders_service.update_order_status(
                    Request({"type": "http"}),
                    order_id,
                    "completed",
                    "card",
                    **kwargs,
                )
        return exc.value

    invalid_type = await _reject(discount_type="bogus", discount_value=10)
    assert invalid_type.status_code == 400
    assert invalid_type.details.get("code") == "discount_type_invalid"

    percent_max = await _reject(discount_type="percent", discount_value=150)
    assert percent_max.details.get("code") == "discount_percent_max"

    too_big = await _reject(discount_type="fixed", discount_value=99999)
    assert too_big.details.get("code") == "discount_exceeds_total"
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_complete_cash_accepts_amount_after_percent_discount():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    order_date = datetime(2026, 8, 19, 12, 0)
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            _pending_row(order_id, total_amount=20000),
            {"id": uuid4()},
            {
                "id": order_id,
                "order_number": 23631,
                "total_amount": 18000,
                "payment_method": "cash",
                "payment_method_id": None,
                "order_date": order_date,
                "tip_amount": 0,
                "tip_tax_amount": 0,
            },
        ]
    )
    conn.execute = AsyncMock()

    patches = _complete_patches(conn, tenant_id, user_id)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
         patches[6], patches[7], patches[8], patches[9], patches[10], patches[11]:
        result = await orders_service.update_order_status(
            Request({"type": "http"}),
            order_id,
            "completed",
            "cash",
            cash_received=18000,
            discount_type="percent",
            discount_value=10,
        )

    assert result["success"] is True
    update_args = conn.execute.await_args_list[0].args
    assert update_args[7] == Decimal("18000")
    assert update_args[13] == 18000.0


@pytest.mark.asyncio
async def test_complete_wallet_debits_discounted_total():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    customer_id = uuid4()
    order_date = datetime(2026, 8, 19, 12, 0)
    apply_wallet = AsyncMock()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            _pending_row(order_id, customer_id=customer_id, total_amount=20000),
            {"id": uuid4()},
            {
                "id": order_id,
                "order_number": 23631,
                "total_amount": 15000,
                "payment_method": "customer_wallet",
                "payment_method_id": None,
                "order_date": order_date,
                "tip_amount": 0,
                "tip_tax_amount": 0,
            },
        ]
    )
    conn.execute = AsyncMock()

    patches = _complete_patches(conn, tenant_id, user_id)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
         patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], \
         patch("app.services.customer_wallet_service.assert_wallet_customer_identified", new=AsyncMock()), \
         patch("app.services.customer_wallet_service.apply_wallet_for_order", new=apply_wallet):
        result = await orders_service.update_order_status(
            Request({"type": "http"}),
            order_id,
            "completed",
            "customer_wallet",
            customer_id=str(customer_id),
            discount_type="fixed",
            discount_value=5000,
        )

    assert result["success"] is True
    apply_wallet.assert_awaited()
    assert apply_wallet.await_args.args[3] == Decimal("15000")


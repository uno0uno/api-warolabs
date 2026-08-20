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


def _pending_row(order_id, *, customer_id=None, total_amount=20000, table_session_id=None):
    return {
        "id": order_id,
        "status": "pending",
        "order_number": 23631,
        "table_session_id": table_session_id,
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


def _completed_gl_row(order_id, *, total_amount, payment_method="card", tip_amount=0, tip_tax_amount=0):
    return {
        "id": order_id,
        "order_number": 23631,
        "total_amount": total_amount,
        "payment_method": payment_method,
        "payment_method_id": None,
        "order_date": datetime(2026, 8, 19, 12, 0),
        "tip_amount": tip_amount,
        "tip_tax_amount": tip_tax_amount,
    }


@pytest.mark.asyncio
async def test_complete_persists_tip_and_passes_it_to_gl():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    gl_mock = AsyncMock()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            _pending_row(order_id, total_amount=20000),
            {"id": uuid4()},
            _completed_gl_row(order_id, total_amount=20000, tip_amount=2000, tip_tax_amount=0),
        ]
    )
    conn.fetchval = AsyncMock(return_value=True)
    conn.execute = AsyncMock()

    patches = list(_complete_patches(conn, tenant_id, user_id))
    patches[7] = patch("app.services.orders_service._post_order_gl_entry", new=gl_mock)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
         patches[6], patches[7], patches[8], patches[9], patches[10], patches[11]:
        result = await orders_service.update_order_status(
            Request({"type": "http"}),
            order_id,
            "completed",
            "card",
            tip_amount=2000,
            tip_source="custom",
            tip_taxable=False,
        )

    assert result["success"] is True
    update_args = conn.execute.await_args_list[0].args
    assert update_args[14] == 2000.0
    assert update_args[15] == "custom"
    assert update_args[16] is False
    assert update_args[17] == 0.0
    assert gl_mock.await_args.kwargs["tip_amount"] == Decimal("2000")
    assert gl_mock.await_args.kwargs["tip_tax_amount"] == Decimal("0")


@pytest.mark.asyncio
async def test_complete_omitted_tip_stays_zero():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            _pending_row(order_id, total_amount=20000),
            {"id": uuid4()},
            _completed_gl_row(order_id, total_amount=20000),
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

    update_args = conn.execute.await_args_list[0].args
    assert update_args[14] == 0.0
    assert update_args[15] == "none"
    assert update_args[16] is False
    assert update_args[17] == 0.0


@pytest.mark.asyncio
async def test_complete_tip_rejected_when_tenant_disabled():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=_pending_row(order_id, total_amount=20000))
    conn.fetchval = AsyncMock(return_value=False)
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
                tip_amount=2000,
                tip_source="preset",
            )

    assert exc.value.status_code == 400
    assert exc.value.details.get("code") == "tip_disabled"
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_complete_tip_invalid_source_is_400():
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
                "card",
                tip_amount=2000,
                tip_source="none",
            )

    assert exc.value.status_code == 400
    assert exc.value.details.get("code") == "tip_invalid"
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_complete_cash_must_cover_product_plus_tip():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=_pending_row(order_id, total_amount=20000))
    conn.fetchval = AsyncMock(return_value=True)
    conn.execute = AsyncMock()

    with patch("app.services.orders_service.require_valid_session", return_value=_session(tenant_id, user_id)), \
         patch("app.services.orders_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.orders_service.resolve_tenant_timezone", new=AsyncMock(return_value="America/Bogota")), \
         patch("app.services.orders_service.assert_order_not_in_closed_monthly_period", new=AsyncMock()), \
         patch("app.services.orders_service.assert_order_invoice_allows_mutation", new=AsyncMock()), \
         patch("app.services.orders_service._get_tenant_tax_config", new=AsyncMock(return_value={"inc_enabled": True})):
        with pytest.raises(APIError) as exc:
            await orders_service.update_order_status(
                Request({"type": "http"}),
                order_id,
                "completed",
                "cash",
                cash_received=20000,
                tip_amount=2000,
                tip_source="custom",
            )

    assert exc.value.details.get("code") == "cash_received_short"
    conn.execute.assert_not_called()

    conn.fetchrow = AsyncMock(
        side_effect=[
            _pending_row(order_id, total_amount=20000),
            {"id": uuid4()},
            _completed_gl_row(order_id, total_amount=20000, payment_method="cash", tip_amount=2000),
        ]
    )
    patches = _complete_patches(conn, tenant_id, user_id)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
         patches[6], patches[7], patches[8], patches[9], patches[10], patches[11]:
        result = await orders_service.update_order_status(
            Request({"type": "http"}),
            order_id,
            "completed",
            "cash",
            cash_received=22000,
            tip_amount=2000,
            tip_source="custom",
        )

    assert result["success"] is True
    update_args = conn.execute.await_args_list[0].args
    assert update_args[7] == Decimal("22000")
    assert update_args[14] == 2000.0


@pytest.mark.asyncio
async def test_complete_one_shot_payments_must_equal_due():
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
                payments=[
                    {"amount": 5000, "payment_method": "cash", "cash_received": 5000},
                    {"amount": 5000, "payment_method": "card"},
                ],
            )

    assert exc.value.status_code == 400
    assert exc.value.details.get("code") == "split_sum_mismatch"
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_complete_one_shot_payments_inserts_tenders():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    payment_a = uuid4()
    payment_b = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            _pending_row(order_id, total_amount=20000),
            {"id": uuid4()},
            {"id": payment_a},
            {"id": payment_b},
            _completed_gl_row(order_id, total_amount=20000),
        ]
    )
    conn.fetch = AsyncMock(return_value=[
        {"amount": 8000, "payment_method": "cash", "payment_method_id": None},
        {"amount": 12000, "payment_method": "card", "payment_method_id": None},
    ])
    conn.execute = AsyncMock()

    patches = list(_complete_patches(conn, tenant_id, user_id))
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
         patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], \
         patch("app.services.credit_service.sync_order_split_credit_status", new=AsyncMock(return_value="paid")):
        result = await orders_service.update_order_status(
            Request({"type": "http"}),
            order_id,
            "completed",
            "cash",
            payments=[
                {"amount": 8000, "payment_method": "cash", "cash_received": 8000},
                {"amount": 12000, "payment_method": "card"},
            ],
        )

    assert result["success"] is True
    inserted_amounts = [
        call.args[3]
        for call in conn.fetchrow.await_args_list
        if call.args and call.args[0].strip().startswith("INSERT INTO order_payments")
    ]
    assert inserted_amounts == [8000.0, 12000.0]


@pytest.mark.asyncio
async def test_complete_sequential_split_keeps_session_open():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    session_id = uuid4()
    gl_mock = AsyncMock()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            _pending_row(order_id, total_amount=20000, table_session_id=session_id),
            {"id": uuid4()},
            {"id": uuid4()},
        ]
    )
    conn.execute = AsyncMock()

    patches = list(_complete_patches(conn, tenant_id, user_id))
    patches[7] = patch("app.services.orders_service._post_order_gl_entry", new=gl_mock)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
         patches[6], patches[7], patches[8], patches[9], patches[10], patches[11]:
        result = await orders_service.update_order_status(
            Request({"type": "http"}),
            order_id,
            "completed",
            "card",
            split_mode=True,
            split_first_amount=5000,
        )

    assert result["success"] is True
    gl_mock.assert_not_awaited()
    update_args = conn.execute.await_args_list[0].args
    assert update_args[6] == "partial"
    session_sql = [
        call.args[0]
        for call in conn.execute.await_args_list
        if "table_sessions" in call.args[0]
    ]
    assert session_sql == []


@pytest.mark.asyncio
async def test_complete_guest_waro_redeem_is_422():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=_pending_row(order_id, total_amount=20000))
    conn.fetch = AsyncMock(return_value=[])
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
                waros_to_redeem=10,
            )

    assert exc.value.status_code == 422
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_complete_applies_waro_reward_after_discount():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    customer_id = uuid4()
    reward_id = uuid4()
    settle = AsyncMock()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            _pending_row(order_id, customer_id=customer_id, total_amount=20000),
            {"id": uuid4()},
            _completed_gl_row(order_id, total_amount=13000),
        ]
    )
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()

    async def _apply(*args, **kwargs):
        return {
            "total_amount": 13000,
            "_waro_redemption_preview": {
                "total_waros_cost": 20,
                "total_waro_discount_cop": 5000,
                "waro_reward_id": str(reward_id),
            },
        }

    patches = list(_complete_patches(conn, tenant_id, user_id))
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
         patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], \
         patch("app.services.waros_service.apply_checkout_waro_redemption", new=_apply), \
         patch("app.services.waros_service.settle_waro_redemption", new=settle):
        result = await orders_service.update_order_status(
            Request({"type": "http"}),
            order_id,
            "completed",
            "card",
            customer_id=str(customer_id),
            discount_type="percent",
            discount_value=10,
            waro_reward_id=reward_id,
        )

    assert result["success"] is True
    settle.assert_awaited()
    update_args = conn.execute.await_args_list[0].args
    assert update_args[13] == 13000


@pytest.mark.asyncio
async def test_void_tender_by_order_id_allows_mesa_row():
    from app.services import pos_cart_service

    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    payment_id = uuid4()
    session_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "id": payment_id,
                "order_id": order_id,
                "amount": 5000,
                "payment_method": "card",
                "cash_received": None,
                "created_by_user_id": user_id,
                "voided_at": None,
                "payment_method_id": None,
                "pos_cart_id": None,
                "total_amount": 20000,
                "tip_amount": 0,
                "tip_tax_amount": 0,
                "payment_status": "partial",
                "order_status": "completed",
                "customer_id": None,
                "table_session_id": session_id,
            },
            {"paid": 5000},
            {"paid": 0},
        ]
    )
    conn.fetchval = AsyncMock(return_value=0)
    conn.execute = AsyncMock()
    conn.transaction = MagicMock(return_value=_AsyncContext())

    with patch("app.services.pos_cart_service.require_valid_session", return_value=_session(tenant_id, user_id)), \
         patch("app.services.pos_cart_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.pos_cart_service.resolve_tenant_timezone", new=AsyncMock(return_value="America/Bogota")), \
         patch("app.services.pos_cart_service._get_tenant_tax_config", new=AsyncMock(return_value={})), \
         patch("app.services.pos_cart_service._additive_tax_for_order", new=AsyncMock(return_value=0)), \
         patch("app.services.pos_cart_service.record_operation_event", new=AsyncMock()), \
         patch("app.services.credit_service.sync_order_split_credit_status", new=AsyncMock(return_value="partial")):
        result = await pos_cart_service.void_order_payment(
            Request({"type": "http"}),
            payment_id=str(payment_id),
            reason="test",
            order_id=str(order_id),
        )

    assert result["success"] is True
    assert result["data"]["remaining"] == 20000
    void_sql = [
        call.args[0]
        for call in conn.execute.await_args_list
        if "voided_at" in call.args[0]
    ]
    assert void_sql



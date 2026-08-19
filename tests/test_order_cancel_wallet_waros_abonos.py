"""Cancel completed sales: block abonos, restore wallet, revoke Waros."""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import Request

from app.core.exceptions import APIError
from app.services import orders_service
from app.services.customer_wallet_service import restore_wallet_for_cancelled_order
from app.services.waros_service import evaluate_and_award, revoke_waros_awarded_for_order


class _AsyncContext:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _session(tenant_id, user_id):
    return SimpleNamespace(tenant_id=tenant_id, user_id=user_id)


def _completed_order(order_id, payment_status="paid", customer_id=None):
    return {
        "id": order_id,
        "status": "completed",
        "order_number": 18401,
        "table_session_id": None,
        "pos_cart_id": None,
        "payment_status": payment_status,
        "order_date": date(2026, 8, 15),
        "total_amount": 45000,
        "customer_id": customer_id,
    }


@pytest.mark.asyncio
async def test_assert_order_has_no_credit_payments_blocks():
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)
    with pytest.raises(APIError) as exc:
        await orders_service.assert_order_has_no_credit_payments(conn, uuid4())
    assert exc.value.status_code == 409
    assert exc.value.details["code"] == "sale_has_credit_payments"


@pytest.mark.asyncio
async def test_assert_order_has_no_credit_payments_allows_zero():
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)
    await orders_service.assert_order_has_no_credit_payments(conn, uuid4())


@pytest.mark.asyncio
async def test_cancel_completed_blocks_when_sale_has_abonos():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=_completed_order(order_id, payment_status="partial"))
    conn.execute = AsyncMock()

    async def fetchval(query, *args):
        if "credit_payments" in query:
            return 2
        return None

    conn.fetchval = AsyncMock(side_effect=fetchval)
    restore = AsyncMock()
    revoke = AsyncMock()
    void = AsyncMock()
    stock = AsyncMock()

    with patch("app.services.orders_service.require_valid_session", return_value=_session(tenant_id, user_id)), \
         patch("app.services.orders_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.orders_service.resolve_tenant_timezone", new=AsyncMock(return_value="America/Bogota")), \
         patch("app.services.orders_service.assert_order_not_in_closed_monthly_period", new=AsyncMock()), \
         patch("app.services.orders_service.restore_wallet_for_cancelled_order", new=restore), \
         patch("app.services.orders_service.revoke_waros_awarded_for_order", new=revoke), \
         patch("app.services.orders_service._return_stock_for_order_cancellation", new=stock), \
         patch("app.services.orders_service._void_order_gl_entries", new=void):
        with pytest.raises(APIError) as exc:
            await orders_service.update_order_status(
                Request({"type": "http"}),
                order_id,
                "cancelled",
                reason="prueba",
            )

    assert exc.value.status_code == 409
    assert exc.value.details["code"] == "sale_has_credit_payments"
    conn.execute.assert_not_called()
    restore.assert_not_called()
    revoke.assert_not_called()
    stock.assert_not_called()
    void.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_credit_without_abonos_restores_wallet_and_waros():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    customer_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=_completed_order(
        order_id, payment_status="credit", customer_id=customer_id,
    ))
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    restore = AsyncMock()
    revoke = AsyncMock()
    void = AsyncMock()
    stock = AsyncMock()

    with patch("app.services.orders_service.require_valid_session", return_value=_session(tenant_id, user_id)), \
         patch("app.services.orders_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.orders_service.resolve_tenant_timezone", new=AsyncMock(return_value="America/Bogota")), \
         patch("app.services.orders_service.assert_order_not_in_closed_monthly_period", new=AsyncMock()), \
         patch("app.services.orders_service.restore_wallet_for_cancelled_order", new=restore), \
         patch("app.services.orders_service.revoke_waros_awarded_for_order", new=revoke), \
         patch("app.services.orders_service._return_stock_for_order_cancellation", new=stock), \
         patch("app.services.orders_service._void_order_gl_entries", new=void), \
         patch("app.services.orders_service.record_operation_event", new=AsyncMock()):
        result = await orders_service.update_order_status(
            Request({"type": "http"}),
            order_id,
            "cancelled",
            reason="sin abonos",
        )

    assert result["success"] is True
    restore.assert_awaited_once()
    revoke.assert_awaited_once_with(conn, order_id, tenant_id)
    stock.assert_awaited_once()
    void.assert_awaited_once()
    cleared = [
        call.args[0] for call in conn.execute.await_args_list
        if "payment_status = NULL" in call.args[0]
    ]
    assert cleared


@pytest.mark.asyncio
async def test_restore_wallet_for_cancelled_order_voids_net_apply():
    tenant_id = uuid4()
    order_id = uuid4()
    profile_id = uuid4()
    payment_id = uuid4()
    movement_id = uuid4()
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[{
        "profile_id": profile_id,
        "net_applied": Decimal("15000"),
        "order_payment_id": payment_id,
    }])
    inserts = []

    async def fetchrow(query, *args):
        q = " ".join(query.split())
        if "phone_number" in q:
            return {"phone_number": "3001234567"}
        if "FOR UPDATE" in q and "customer_wallet_balances" in q:
            return {"balance_cop": Decimal("1000")}
        if "INSERT INTO customer_wallet_movements" in q:
            inserts.append(args)
            return {"id": movement_id}
        return None

    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    conn.execute = AsyncMock()

    restored = await restore_wallet_for_cancelled_order(
        conn, tenant_id, order_id, None, notes="Cancelación venta #18401",
    )

    assert restored == [movement_id]
    assert inserts[0][2] == "void_apply"
    assert inserts[0][3] == Decimal("15000")
    assert inserts[0][7] == order_id
    assert inserts[0][8] == payment_id


@pytest.mark.asyncio
async def test_restore_wallet_for_cancelled_order_skips_when_already_voided():
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[{
        "profile_id": uuid4(),
        "net_applied": Decimal("0"),
        "order_payment_id": uuid4(),
    }])
    conn.fetchrow = AsyncMock()
    restored = await restore_wallet_for_cancelled_order(conn, uuid4(), uuid4(), None)
    assert restored == []
    conn.fetchrow.assert_not_called()


@pytest.mark.asyncio
async def test_revoke_waros_awarded_for_order_inserts_negative_earned():
    tenant_id = uuid4()
    order_id = uuid4()
    profile_id = uuid4()
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[{
        "profile_id": profile_id,
        "net_awarded": 40,
    }])
    conn.fetchrow = AsyncMock(return_value={"current_balance": 55})
    conn.execute = AsyncMock()

    revoked = await revoke_waros_awarded_for_order(conn, order_id, tenant_id)

    assert revoked == 40
    wallet_update = conn.execute.await_args_list[0].args
    assert wallet_update[3] == 40
    tx_insert = conn.execute.await_args_list[1].args
    assert tx_insert[3] == -40
    assert tx_insert[5] == str(order_id)


@pytest.mark.asyncio
async def test_revoke_waros_clamps_to_current_balance():
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[{"profile_id": uuid4(), "net_awarded": 80}])
    conn.fetchrow = AsyncMock(return_value={"current_balance": 10})
    conn.execute = AsyncMock()
    revoked = await revoke_waros_awarded_for_order(conn, uuid4(), uuid4())
    assert revoked == 10
    assert conn.execute.await_args_list[1].args[3] == -10


@pytest.mark.asyncio
async def test_evaluate_and_award_skips_when_order_not_completed():
    tenant_id = uuid4()
    conn = AsyncMock()

    async def fetchrow(query, *args):
        q = " ".join(query.split()).lower()
        if "gamification_config" in q:
            return {
                "is_enabled": True,
                "max_daily_waros": 0,
                "earn_on_wallet_payment": True,
                "earn_base_excludes_waro_redemption": False,
            }
        if "from orders" in q:
            return {
                "status": "cancelled",
                "total_amount": 40_000.0,
                "waro_redeemed_amount_cop": 0.0,
                "payment_method": "cash",
            }
        return None

    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    conn.fetch = AsyncMock()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_db(**kwargs):
        yield conn

    with patch("app.services.waros_service.get_db_connection", side_effect=fake_db):
        awarded = await evaluate_and_award(uuid4(), uuid4(), tenant_id)

    assert awarded == 0
    conn.fetch.assert_not_called()

"""#2020 — split wallet apply + credit receivable for CRM Cartera."""
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.services.credit_service import sync_order_split_credit_status
from app.services import pos_cart_service


@pytest.mark.asyncio
async def test_sync_wallet_plus_credit_keeps_partial_and_seeds_credit_paid():
    order_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"total_amount": 133_000.0, "payment_status": "partial"})
    conn.fetch = AsyncMock(return_value=[
        {"payment_method": "customer_wallet", "amount": 100_000.0},
        {"payment_method": "credit", "amount": 33_000.0},
    ])
    conn.execute = AsyncMock()

    status = await sync_order_split_credit_status(
        conn, order_id, settlement_complete=True,
    )

    assert status == "partial"
    conn.execute.assert_awaited_once()
    args = conn.execute.await_args.args
    assert args[1] == order_id
    assert args[2] == "partial"
    assert args[3] == 100_000.0


@pytest.mark.asyncio
async def test_sync_credit_only_sets_credit_status():
    order_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"total_amount": 50_000.0, "payment_status": "partial"})
    conn.fetch = AsyncMock(return_value=[
        {"payment_method": "credit", "amount": 50_000.0},
    ])
    conn.execute = AsyncMock()

    status = await sync_order_split_credit_status(
        conn, order_id, settlement_complete=True,
    )

    assert status == "credit"
    assert conn.execute.await_args.args[2] == "credit"
    assert conn.execute.await_args.args[3] == 0.0


@pytest.mark.asyncio
async def test_sync_no_credit_mid_split_stays_partial():
    order_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"total_amount": 100_000.0, "payment_status": "partial"})
    conn.fetch = AsyncMock(return_value=[
        {"payment_method": "customer_wallet", "amount": 40_000.0},
    ])
    conn.execute = AsyncMock()

    status = await sync_order_split_credit_status(
        conn, order_id, settlement_complete=False,
    )

    assert status == "partial"
    assert conn.execute.await_args.args[2] == "partial"


@pytest.mark.asyncio
async def test_sync_no_credit_settlement_complete_marks_paid():
    order_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"total_amount": 100_000.0, "payment_status": "partial"})
    conn.fetch = AsyncMock(return_value=[
        {"payment_method": "customer_wallet", "amount": 40_000.0},
        {"payment_method": "cash", "amount": 60_000.0},
    ])
    conn.execute = AsyncMock()

    status = await sync_order_split_credit_status(
        conn, order_id, settlement_complete=True,
    )

    assert status == "paid"


@pytest.mark.asyncio
async def test_add_order_payment_final_credit_tender_not_paid():
    """Completing a split with credit must not force payment_status=paid."""
    tenant_id = uuid4()
    user_id = uuid4()
    cart_id = uuid4()
    order_id = uuid4()
    payment_id = uuid4()
    customer_id = uuid4()

    mock_conn = AsyncMock()
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_cm.__aexit__ = AsyncMock(return_value=None)

    class _Txn:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *args):
            return None

    mock_conn.transaction = MagicMock(return_value=_Txn())
    mock_conn.fetchrow = AsyncMock(side_effect=[
        {"id": cart_id, "tenant_id": tenant_id},
        {
            "id": order_id,
            "total_amount": 100.0,
            "tip_amount": 0,
            "tip_source": "none",
            "tip_taxable": False,
            "tip_tax_amount": 0,
            "status": "active",
            "payment_status": "partial",
            "customer_id": customer_id,
            "order_number": 17449,
        },
        {"paid_total": 70.0},
        {"id": payment_id},
        {"paid_total": 100.0},
    ])
    mock_conn.fetchval = AsyncMock(return_value=customer_id)
    mock_conn.execute = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=[])

    with patch("app.services.pos_cart_service.require_valid_session") as mock_sess, \
         patch("app.services.pos_cart_service.get_db_connection", return_value=mock_cm), \
         patch("app.services.pos_cart_service._get_tenant_tax_config", new=AsyncMock(return_value={})), \
         patch("app.services.pos_cart_service._additive_tax_for_order", new=AsyncMock(return_value=0.0)), \
         patch(
             "app.services.credit_service.sync_order_split_credit_status",
             new=AsyncMock(return_value="partial"),
         ) as sync_credit, \
         patch("app.services.pos_cart_service.finalize_open_comandas", new=AsyncMock()):
        mock_sess.return_value = MagicMock(tenant_id=tenant_id, user_id=user_id)
        result = await pos_cart_service.add_order_payment(
            MagicMock(),
            str(cart_id),
            amount=30.0,
            payment_method="credit",
        )

    assert result["data"]["is_complete"] is True
    assert result["data"]["payment_status"] == "partial"
    sync_credit.assert_awaited_once()
    assert sync_credit.await_args.kwargs.get("settlement_complete") is True
    status_updates = [
        c.args[0] for c in mock_conn.execute.await_args_list
        if isinstance(c.args[0], str) and "UPDATE orders" in c.args[0]
    ]
    assert any("payment_method" in sql for sql in status_updates)
    assert not any("payment_status = 'paid'" in sql for sql in status_updates)
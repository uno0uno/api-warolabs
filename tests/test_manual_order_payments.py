from datetime import datetime
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


def _conn_for_manual_order(*, order_id, order_item_id, order_date, payment_ids=()):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "id": order_id,
                "order_number": 41010,
                "order_date": order_date,
                "created_at": order_date,
            },
            {"id": order_item_id},
            *[{"id": payment_id} for payment_id in payment_ids],
        ]
    )
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()
    conn.transaction = MagicMock(return_value=_AsyncContext())
    return conn


def _patch_manual_order(conn, tenant_id, user_id):
    return (
        patch(
            "app.services.orders_service.require_valid_session",
            return_value=SimpleNamespace(tenant_id=tenant_id, user_id=user_id),
        ),
        patch("app.services.orders_service.get_db_connection", return_value=_AsyncContext(conn)),
        patch("app.services.orders_service.assert_order_not_in_closed_monthly_period", new=AsyncMock()),
        patch("app.services.orders_service._pos_modifier_inventory_helpers", return_value=(AsyncMock(), None, None)),
        patch("app.services.orders_service._pos_order_item_ingredient_snapshot_helper", return_value=AsyncMock()),
        patch("app.services.orders_service._get_tenant_tax_config", new=AsyncMock(return_value={"inc_enabled": True})),
        patch("app.services.orders_service._post_order_gl_entry", new=AsyncMock()),
        patch("app.services.orders_service._post_order_cogs_gl_entry", new=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_create_manual_order_persists_percent_discount_server_side():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    order_item_id = uuid4()
    product_id = uuid4()
    order_date = datetime(2026, 6, 19, 9, 0)
    conn = _conn_for_manual_order(
        order_id=order_id,
        order_item_id=order_item_id,
        order_date=order_date,
    )

    patches = _patch_manual_order(conn, tenant_id, user_id)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
        result = await orders_service.create_manual_order(
            Request({"type": "http"}),
            order_date="2026-06-19T09:00:00",
            payment_method="cash",
            items=[
                {
                    "product_id": str(product_id),
                    "quantity": 2,
                    "unit_price": 5000,
                    "modifiers": [],
                }
            ],
            discount_type="percent",
            discount_value=10,
        )

    order_insert_args = conn.fetchrow.await_args_list[0].args
    assert result["data"]["total_amount"] == 9000
    assert result["data"]["discount_amount"] == 1000
    assert order_insert_args[6] == 9000
    assert order_insert_args[7] == "percent"
    assert order_insert_args[8] == 10
    assert order_insert_args[9] == 1000


@pytest.mark.asyncio
async def test_create_manual_order_persists_split_payments_and_wallet_debit():
    tenant_id = uuid4()
    user_id = uuid4()
    customer_id = uuid4()
    order_id = uuid4()
    order_item_id = uuid4()
    first_payment_id = uuid4()
    wallet_payment_id = uuid4()
    product_id = uuid4()
    order_date = datetime(2026, 6, 19, 9, 0)
    conn = _conn_for_manual_order(
        order_id=order_id,
        order_item_id=order_item_id,
        order_date=order_date,
        payment_ids=(first_payment_id, wallet_payment_id),
    )
    apply_wallet = AsyncMock()

    patches = _patch_manual_order(conn, tenant_id, user_id)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patch(
        "app.services.customer_wallet_service.apply_wallet_for_order",
        new=apply_wallet,
    ), patch(
        "app.services.orders_service.evaluate_and_award",
        new=MagicMock(return_value=object()),
    ), patch(
        "app.services.orders_service.asyncio.create_task",
        new=MagicMock(),
    ):
        result = await orders_service.create_manual_order(
            Request({"type": "http"}),
            order_date="2026-06-19T09:00:00",
            payment_method="cash",
            items=[
                {
                    "product_id": str(product_id),
                    "quantity": 1,
                    "unit_price": 30000,
                    "modifiers": [],
                }
            ],
            customer_id=str(customer_id),
            payments=[
                {"amount": 20000, "payment_method": "cash", "payment_method_id": None},
                {"amount": 10000, "payment_method": "customer_wallet", "payment_method_id": None},
            ],
        )

    payment_inserts = [
        call.args for call in conn.fetchrow.await_args_list
        if "INSERT INTO order_payments" in call.args[0]
    ]
    assert result["success"] is True
    assert len(payment_inserts) == 2
    assert payment_inserts[0][3] == 20000
    assert payment_inserts[0][4] == "cash"
    assert payment_inserts[1][3] == 10000
    assert payment_inserts[1][4] == "customer_wallet"
    apply_wallet.assert_awaited_once()
    assert apply_wallet.await_args.args[1] == customer_id
    assert apply_wallet.await_args.args[3].to_integral_value() == 10000
    assert apply_wallet.await_args.args[6] == wallet_payment_id


@pytest.mark.asyncio
async def test_create_manual_order_rejects_split_total_mismatch():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    order_item_id = uuid4()
    product_id = uuid4()
    order_date = datetime(2026, 6, 19, 9, 0)
    conn = _conn_for_manual_order(
        order_id=order_id,
        order_item_id=order_item_id,
        order_date=order_date,
    )

    patches = _patch_manual_order(conn, tenant_id, user_id)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
        with pytest.raises(APIError, match="pagos divididos"):
            await orders_service.create_manual_order(
                Request({"type": "http"}),
                order_date="2026-06-19T09:00:00",
                payment_method="cash",
                items=[
                    {
                        "product_id": str(product_id),
                        "quantity": 1,
                        "unit_price": 30000,
                        "modifiers": [],
                    }
                ],
                payments=[
                    {"amount": 10000, "payment_method": "cash", "payment_method_id": None},
                ],
            )


@pytest.mark.asyncio
async def test_create_manual_order_rejects_wallet_without_customer_before_db():
    with patch(
        "app.services.orders_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=uuid4(), user_id=uuid4()),
    ):
        with pytest.raises(APIError, match="cliente identificado"):
            await orders_service.create_manual_order(
                Request({"type": "http", "headers": []}),
                order_date="2026-06-19T09:00:00",
                payment_method="cash",
                items=[
                    {
                        "product_id": str(uuid4()),
                        "quantity": 1,
                        "unit_price": 10000,
                        "modifiers": [],
                    }
                ],
                payments=[
                    {"amount": 10000, "payment_method": "customer_wallet", "payment_method_id": None},
                ],
            )

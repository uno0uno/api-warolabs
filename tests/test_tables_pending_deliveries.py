from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.core.exceptions import APIError
from app.services import tables_service


def _db_context(conn):
    @asynccontextmanager
    async def _ctx(*_args, **_kwargs):
        yield conn

    return _ctx


def _session(tenant_id=None):
    return SimpleNamespace(user_id=uuid4(), tenant_id=tenant_id or uuid4())


def _pending_row(*, order_id, customer_id, address_id):
    return {
        "id": order_id,
        "order_number": 42,
        "order_date": datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc),
        "total_amount": 25000,
        "status": "pending",
        "payment_status": None,
        "delivery_instructions": "Portería",
        "delivery_address_id": address_id,
        "customer_id": customer_id,
        "customer_name": "Ana",
        "customer_phone": "3001234567",
        "address_line1": "Cra 50 #10-20",
        "address_line2": None,
        "city": "Bogotá",
    }


@pytest.mark.asyncio
async def test_list_pending_deliveries_filters_unpaid_delivery_orders():
    tenant_id = uuid4()
    order_id = uuid4()
    customer_id = uuid4()
    address_id = uuid4()
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[
        _pending_row(order_id=order_id, customer_id=customer_id, address_id=address_id),
    ])

    with (
        patch("app.services.tables_service.require_valid_session", return_value=_session(tenant_id)),
        patch("app.services.tables_service.get_db_connection", side_effect=_db_context(conn)),
    ):
        result = await tables_service.list_pending_deliveries(object())

    query = conn.fetch.await_args.args[0]
    assert "o.delivery_address_id IS NOT NULL" in query
    assert "order_payments op" in query
    assert "o.status NOT IN ('cancelled', 'refunded')" in query
    assert "o.payment_status = 'paid'" in query
    assert "t.is_bar = TRUE" in query
    assert result["success"] is True
    assert result["data"][0]["id"] == str(order_id)
    assert result["data"][0]["address_label"] == "Cra 50 #10-20, Bogotá"
    assert result["data"][0]["customer"]["name"] == "Ana"


@pytest.mark.asyncio
async def test_get_pending_delivery_rejects_paid_orders():
    order_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[
        {"payment_count": 1},
        {"paid_total": 180000},
    ])
    with (
        patch(
            "app.services.tables_service.get_order_by_id",
            new=AsyncMock(return_value={
                "success": True,
                "data": {
                    "id": str(order_id),
                    "status": "completed",
                    "is_delivery": True,
                    "delivery_address_id": str(uuid4()),
                    "payment_status": "paid",
                    "source": "barra",
                    "total_amount": 180000,
                    "tip_amount": 0,
                    "tip_tax_amount": 0,
                },
            }),
        ),
        patch("app.services.tables_service.require_valid_session", return_value=_session()),
        patch("app.services.tables_service.get_db_connection", side_effect=_db_context(conn)),
    ):
        with pytest.raises(APIError, match="pendiente"):
            await tables_service.get_pending_delivery(object(), order_id)


@pytest.mark.asyncio
async def test_get_pending_delivery_rejects_fully_paid_credit_split():
    """Credit-mixed split keeps payment_status=partial for Cartera but leaves the POS queue."""
    order_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[
        {"payment_count": 3},
        {"paid_total": 180000},
    ])
    with (
        patch(
            "app.services.tables_service.get_order_by_id",
            new=AsyncMock(return_value={
                "success": True,
                "data": {
                    "id": str(order_id),
                    "status": "completed",
                    "is_delivery": True,
                    "delivery_address_id": str(uuid4()),
                    "payment_status": "partial",
                    "source": "barra",
                    "total_amount": 180000,
                    "tip_amount": 0,
                    "tip_tax_amount": 0,
                },
            }),
        ),
        patch("app.services.tables_service.require_valid_session", return_value=_session()),
        patch("app.services.tables_service.get_db_connection", side_effect=_db_context(conn)),
    ):
        with pytest.raises(APIError, match="pendiente"):
            await tables_service.get_pending_delivery(object(), order_id)


@pytest.mark.asyncio
async def test_get_pending_delivery_rejects_non_bar_orders():
    order_id = uuid4()
    with patch(
        "app.services.tables_service.get_order_by_id",
        new=AsyncMock(return_value={
            "success": True,
            "data": {
                "id": str(order_id),
                "status": "pending",
                "is_delivery": True,
                "delivery_address_id": str(uuid4()),
                "payment_status": None,
                "source": "pos",
            },
        }),
    ):
        with pytest.raises(APIError, match="pendiente"):
            await tables_service.get_pending_delivery(object(), order_id)


@pytest.mark.asyncio
async def test_complete_pending_delivery_marks_order_completed():
    order_id = uuid4()
    customer_id = uuid4()
    order_data = {
        "id": str(order_id),
        "order_number": 42,
        "total_amount": 25000,
        "status": "pending",
        "is_delivery": True,
        "delivery_address_id": str(uuid4()),
        "payment_status": None,
        "customer": {"id": str(customer_id)},
        "source": "barra",
        "items": [],
        "standard_tax": 0,
        "liquor_tax": 0,
    }

    with (
        patch(
            "app.services.tables_service.get_pending_delivery",
            new=AsyncMock(return_value={"success": True, "data": order_data}),
        ),
        patch(
            "app.services.tables_service.update_order_status",
            new=AsyncMock(return_value={"success": True, "message": "Estado actualizado a completed"}),
        ) as update,
        patch("app.services.tables_service.require_valid_session", return_value=_session()),
        patch("app.services.tables_service.get_db_connection", side_effect=_db_context(MagicMock(
            execute=AsyncMock(),
            fetchrow=AsyncMock(side_effect=[
                {
                    "total_amount": 25000,
                    "tip_amount": 0,
                    "tip_tax_amount": 0,
                    "status": "completed",
                    "payment_status": "paid",
                },
                {"paid": 25000},
                {"id": uuid4()},
            ]),
        ))),
    ):
        result = await tables_service.complete_pending_delivery(
            object(),
            order_id,
            payment_method="cash",
            cash_received=30000,
        )

    update.assert_awaited_once()
    assert update.await_args.args[2] == "completed"
    assert result["data"]["order_id"] == str(order_id)
    assert result["data"]["payment_method"] == "cash"


@pytest.mark.asyncio
async def test_get_pending_delivery_rejects_zombie_paid_without_payments():
    order_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"payment_count": 0})

    with (
        patch(
            "app.services.tables_service.get_order_by_id",
            new=AsyncMock(return_value={
                "success": True,
                "data": {
                    "id": str(order_id),
                    "status": "completed",
                    "is_delivery": True,
                    "delivery_address_id": str(uuid4()),
                    "payment_status": "paid",
                    "source": "barra",
                    "total_amount": 71000,
                    "tip_amount": 0,
                    "tip_tax_amount": 0,
                },
            }),
        ),
        patch("app.services.tables_service.require_valid_session", return_value=_session()),
        patch("app.services.tables_service.get_db_connection", side_effect=_db_context(conn)),
    ):
        with pytest.raises(APIError, match="pagos registrados"):
            await tables_service.get_pending_delivery(object(), order_id)


@pytest.mark.asyncio
async def test_get_pending_delivery_accepts_partial_completed():
    order_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[
        {"payment_count": 1},
        {"paid_total": 50000},
    ])
    conn.fetch = AsyncMock(return_value=[])

    with (
        patch(
            "app.services.tables_service.get_order_by_id",
            new=AsyncMock(return_value={
                "success": True,
                "data": {
                    "id": str(order_id),
                    "status": "completed",
                    "is_delivery": True,
                    "delivery_address_id": str(uuid4()),
                    "payment_status": "partial",
                    "source": "barra",
                    "total_amount": 180000,
                    "tip_amount": 0,
                    "tip_tax_amount": 0,
                },
            }),
        ),
        patch(
            "app.services.tables_service.get_order_items",
            new=AsyncMock(return_value={"success": True, "data": []}),
        ),
        patch("app.services.tables_service.require_valid_session", return_value=_session()),
        patch("app.services.tables_service.get_db_connection", side_effect=_db_context(conn)),
    ):
        result = await tables_service.get_pending_delivery(object(), order_id)

    assert result["success"] is True
    assert result["data"]["partial_payments"] == []

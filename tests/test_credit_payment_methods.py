from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.core.exceptions import APIError
from app.services import credit_service


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _request():
    return MagicMock()


def _session(tenant_id, user_id):
    return SimpleNamespace(tenant_id=tenant_id, user_id=user_id)


def _credit_order(tenant_id, customer_id):
    return {
        "id": uuid4(),
        "tenant_id": tenant_id,
        "customer_id": customer_id,
        "total_amount": Decimal("100.00"),
        "payment_status": "credit",
        "credit_paid_amount": Decimal("25.00"),
    }


@pytest.mark.asyncio
async def test_register_credit_payment_base_method_persists_null_method_id():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    customer_id = uuid4()
    payment_id = uuid4()
    group_id = uuid4()
    payment_date = datetime(2026, 6, 8, tzinfo=timezone.utc)

    conn = MagicMock()
    conn.transaction.return_value = _AsyncContext(None)
    conn.fetchrow = AsyncMock(side_effect=[
        _credit_order(tenant_id, customer_id),
        {"id": group_id},
        {"id": payment_id, "payment_date": payment_date, "created_at": payment_date},
    ])
    conn.execute = AsyncMock()

    with patch(
        "app.services.credit_service.require_valid_session",
        return_value=_session(tenant_id, user_id),
    ), patch(
        "app.services.credit_service.get_db_connection",
        return_value=_AsyncContext(conn),
    ):
        result = await credit_service.register_credit_payment(
            _request(),
            order_id,
            Decimal("20.00"),
            "cash",
        )

    assert result["data"]["payment_method"] == "cash"
    assert result["data"]["payment_method_id"] is None
    insert_args = conn.fetchrow.await_args_list[2].args
    assert insert_args[5] == "cash"
    assert insert_args[6] is None
    conn.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_register_credit_payment_custom_method_validates_and_persists_id():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    customer_id = uuid4()
    group_id = uuid4()
    method_id = uuid4()
    payment_id = uuid4()
    payment_date = datetime(2026, 6, 8, tzinfo=timezone.utc)

    conn = MagicMock()
    conn.transaction.return_value = _AsyncContext(None)
    conn.fetchrow = AsyncMock(side_effect=[
        _credit_order(tenant_id, customer_id),
        {"id": group_id},
        {"id": method_id},
        {"id": payment_id, "payment_date": payment_date, "created_at": payment_date},
    ])
    conn.execute = AsyncMock()

    with patch(
        "app.services.credit_service.require_valid_session",
        return_value=_session(tenant_id, user_id),
    ), patch(
        "app.services.credit_service.get_db_connection",
        return_value=_AsyncContext(conn),
    ):
        result = await credit_service.register_credit_payment(
            _request(),
            order_id,
            Decimal("20.00"),
            "digital",
            method_id,
        )

    assert result["data"]["payment_method_id"] == str(method_id)
    method_lookup_args = conn.fetchrow.await_args_list[2].args
    assert method_lookup_args[1:] == (method_id, tenant_id, group_id)
    insert_args = conn.fetchrow.await_args_list[3].args
    assert insert_args[5] == "digital"
    assert insert_args[6] == method_id
    conn.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_register_credit_payment_rejects_invalid_method_id_before_insert():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    customer_id = uuid4()
    group_id = uuid4()
    method_id = uuid4()

    conn = MagicMock()
    conn.transaction.return_value = _AsyncContext(None)
    conn.fetchrow = AsyncMock(side_effect=[
        _credit_order(tenant_id, customer_id),
        {"id": group_id},
        None,
    ])
    conn.execute = AsyncMock()

    with patch(
        "app.services.credit_service.require_valid_session",
        return_value=_session(tenant_id, user_id),
    ), patch(
        "app.services.credit_service.get_db_connection",
        return_value=_AsyncContext(conn),
    ):
        with pytest.raises(APIError) as exc:
            await credit_service.register_credit_payment(
                _request(),
                order_id,
                Decimal("20.00"),
                "digital",
                method_id,
            )

    assert exc.value.status_code == 400
    assert exc.value.details["code"] == "payment_method_id_invalid"
    assert conn.fetchrow.await_count == 3
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_register_credit_payment_rejects_invalid_group_before_insert():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    customer_id = uuid4()

    conn = MagicMock()
    conn.transaction.return_value = _AsyncContext(None)
    conn.fetchrow = AsyncMock(side_effect=[
        _credit_order(tenant_id, customer_id),
        None,
    ])
    conn.execute = AsyncMock()

    with patch(
        "app.services.credit_service.require_valid_session",
        return_value=_session(tenant_id, user_id),
    ), patch(
        "app.services.credit_service.get_db_connection",
        return_value=_AsyncContext(conn),
    ):
        with pytest.raises(APIError) as exc:
            await credit_service.register_credit_payment(
                _request(),
                order_id,
                Decimal("20.00"),
                "not-a-method",
            )

    assert exc.value.status_code == 400
    assert exc.value.details["code"] == "payment_method_invalid"
    assert conn.fetchrow.await_count == 2
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_credit_payments_returns_payment_method_id():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    method_id = uuid4()
    payment_id = uuid4()
    payment_date = datetime(2026, 6, 8, tzinfo=timezone.utc)

    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={
        "id": order_id,
        "total_amount": Decimal("100.00"),
        "payment_status": "partial",
        "credit_paid_amount": Decimal("40.00"),
    })
    conn.fetch = AsyncMock(return_value=[{
        "id": payment_id,
        "amount": Decimal("40.00"),
        "payment_method": "digital",
        "payment_method_id": method_id,
        "payment_date": payment_date,
        "notes": "Nequi",
        "created_at": payment_date,
        "created_by_user_id": user_id,
    }])

    with patch(
        "app.services.credit_service.require_valid_session",
        return_value=_session(tenant_id, user_id),
    ), patch(
        "app.services.credit_service.get_db_connection",
        return_value=_AsyncContext(conn),
    ):
        result = await credit_service.get_credit_payments(_request(), order_id)

    assert result["data"]["payments"][0]["payment_method"] == "digital"
    assert result["data"]["payments"][0]["payment_method_id"] == str(method_id)

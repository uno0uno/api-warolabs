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
async def test_credit_payment_debit_code_uses_group_puc_when_method_has_no_puc():
    tenant_id = uuid4()
    group_id = uuid4()
    method_id = uuid4()
    conn = MagicMock()
    conn.fetchval = AsyncMock(side_effect=["112010"])

    code = await credit_service._resolve_credit_payment_debit_code(
        conn,
        tenant_id,
        "digital",
        group_id,
        method_id,
    )

    assert code == "112010"
    lookup_args = conn.fetchval.await_args.args
    assert lookup_args[1:] == (method_id, tenant_id, group_id)


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
        {"id": uuid4()},
    ])
    conn.fetchval = AsyncMock(side_effect=[None, None, uuid4(), uuid4()])
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
    assert conn.execute.await_count == 3


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
        {"id": uuid4()},
    ])
    conn.fetchval = AsyncMock(side_effect=[None, "112005", uuid4(), uuid4()])
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
    assert conn.execute.await_count == 3


@pytest.mark.asyncio
async def test_register_credit_payment_posts_gl_with_custom_method_puc():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    customer_id = uuid4()
    group_id = uuid4()
    method_id = uuid4()
    payment_id = uuid4()
    journal_id = uuid4()
    debit_account_id = uuid4()
    credit_account_id = uuid4()
    payment_date = datetime(2026, 6, 8, tzinfo=timezone.utc)

    conn = MagicMock()
    conn.transaction.return_value = _AsyncContext(None)
    conn.fetchrow = AsyncMock(side_effect=[
        _credit_order(tenant_id, customer_id),
        {"id": group_id},
        {"id": method_id},
        {"id": payment_id, "payment_date": payment_date, "created_at": payment_date},
        {"id": journal_id},
    ])
    conn.fetchval = AsyncMock(side_effect=[
        None,
        "112005",
        debit_account_id,
        credit_account_id,
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

    assert result["data"]["payment_id"] == str(payment_id)
    entry_args = conn.fetchrow.await_args_list[4].args
    assert entry_args[1:5] == (tenant_id, payment_date.date(), 2026, 6)
    assert entry_args[7] == payment_id
    assert entry_args[8] == 20.0
    assert entry_args[9] == user_id
    debit_args = conn.execute.await_args_list[1].args
    credit_args = conn.execute.await_args_list[2].args
    assert debit_args[1:5] == (
        journal_id,
        debit_account_id,
        20.0,
        "Dr 112005 - abono cartera",
    )
    assert credit_args[1:5] == (
        journal_id,
        credit_account_id,
        20.0,
        "Cr 1305 - clientes por cobrar",
    )


@pytest.mark.asyncio
async def test_register_credit_payment_gl_uses_slug_fallback_without_puc():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    customer_id = uuid4()
    group_id = uuid4()
    payment_id = uuid4()
    debit_account_id = uuid4()
    credit_account_id = uuid4()
    payment_date = datetime(2026, 6, 8, tzinfo=timezone.utc)

    conn = MagicMock()
    conn.transaction.return_value = _AsyncContext(None)
    conn.fetchrow = AsyncMock(side_effect=[
        _credit_order(tenant_id, customer_id),
        {"id": group_id},
        {"id": payment_id, "payment_date": payment_date, "created_at": payment_date},
        {"id": uuid4()},
    ])
    conn.fetchval = AsyncMock(side_effect=[
        None,
        None,
        debit_account_id,
        credit_account_id,
    ])
    conn.execute = AsyncMock()

    with patch(
        "app.services.credit_service.require_valid_session",
        return_value=_session(tenant_id, user_id),
    ), patch(
        "app.services.credit_service.get_db_connection",
        return_value=_AsyncContext(conn),
    ):
        await credit_service.register_credit_payment(
            _request(),
            order_id,
            Decimal("20.00"),
            "digital",
        )

    debit_account_lookup_args = conn.fetchval.await_args_list[2].args
    credit_account_lookup_args = conn.fetchval.await_args_list[3].args
    assert debit_account_lookup_args[1:] == (tenant_id, "1110")
    assert credit_account_lookup_args[1:] == (tenant_id, "1305")
    debit_args = conn.execute.await_args_list[1].args
    credit_args = conn.execute.await_args_list[2].args
    assert debit_args[3] == 20.0
    assert credit_args[3] == 20.0


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

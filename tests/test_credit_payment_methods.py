from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.core.exceptions import APIError
from app.services import credit_service
from app.services.account_role_service import AccountRef, AccountRole


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
async def test_credit_payment_debit_account_uses_semantic_payment_resolver():
    tenant_id = uuid4()
    group_id = uuid4()
    method_id = uuid4()
    expected = AccountRef(uuid4(), "BANK-01", "Settlement", AccountRole.BANK, "tenant_override")

    with patch(
        "app.services.credit_service.resolve_payment_account",
        new=AsyncMock(return_value=expected),
    ) as resolver:
        account = await credit_service._resolve_credit_payment_debit_account(
            MagicMock(), tenant_id, "digital", group_id, method_id
        )

    assert account == expected
    resolver.assert_awaited_once_with(
        resolver.await_args.args[0],
        tenant_id,
        "digital",
        payment_method_id=method_id,
        payment_group_id=group_id,
        source="credit_payment",
    )


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
    ), patch(
        "app.services.credit_service.resolve_payment_account",
        new=AsyncMock(return_value=AccountRef(uuid4(), "CASH", "Cash", AccountRole.CASH, "localization_default")),
    ), patch(
        "app.services.credit_service.resolve_account",
        new=AsyncMock(return_value=AccountRef(uuid4(), "AR", "Receivable", AccountRole.ACCOUNTS_RECEIVABLE, "localization_default")),
    ), patch(
        "app.services.credit_service.record_operation_event",
        new=AsyncMock(),
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
    ), patch(
        "app.services.credit_service.resolve_payment_account",
        new=AsyncMock(return_value=AccountRef(uuid4(), "BANK", "Bank", AccountRole.BANK, "tenant_override")),
    ), patch(
        "app.services.credit_service.resolve_account",
        new=AsyncMock(return_value=AccountRef(uuid4(), "AR", "Receivable", AccountRole.ACCOUNTS_RECEIVABLE, "localization_default")),
    ), patch(
        "app.services.credit_service.record_operation_event",
        new=AsyncMock(),
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
    ), patch(
        "app.services.credit_service.resolve_payment_account",
        new=AsyncMock(return_value=AccountRef(debit_account_id, "112005", "Bank", AccountRole.BANK, "tenant_override")),
    ), patch(
        "app.services.credit_service.resolve_account",
        new=AsyncMock(return_value=AccountRef(credit_account_id, "1305", "Receivable", AccountRole.ACCOUNTS_RECEIVABLE, "localization_default")),
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
    ), patch(
        "app.services.credit_service.resolve_payment_account",
        new=AsyncMock(return_value=AccountRef(debit_account_id, "BANK", "Bank", AccountRole.BANK, "localization_default")),
    ), patch(
        "app.services.credit_service.resolve_account",
        new=AsyncMock(return_value=AccountRef(credit_account_id, "AR", "Receivable", AccountRole.ACCOUNTS_RECEIVABLE, "localization_default")),
    ):
        await credit_service.register_credit_payment(
            _request(),
            order_id,
            Decimal("20.00"),
            "digital",
        )

    debit_args = conn.execute.await_args_list[1].args
    credit_args = conn.execute.await_args_list[2].args
    assert debit_args[2] == debit_account_id
    assert credit_args[2] == credit_account_id
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


@pytest.mark.asyncio
async def test_register_credit_payment_wallet_applies_before_insert():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    customer_id = uuid4()
    payment_id = uuid4()
    group_id = uuid4()
    payment_date = datetime(2026, 6, 8, tzinfo=timezone.utc)
    apply_mock = AsyncMock(return_value=uuid4())

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
    ), patch(
        "app.services.credit_service.resolve_payment_account",
        new=AsyncMock(
            return_value=AccountRef(
                uuid4(), "2805", "Anticipos", AccountRole.CUSTOMER_ADVANCES, "localization_default"
            )
        ),
    ), patch(
        "app.services.credit_service.resolve_account",
        new=AsyncMock(
            return_value=AccountRef(
                uuid4(), "AR", "Receivable", AccountRole.ACCOUNTS_RECEIVABLE, "localization_default"
            )
        ),
    ), patch(
        "app.services.customer_wallet_service.apply_wallet_for_order",
        apply_mock,
    ), patch(
        "app.services.credit_service.record_operation_event",
        new=AsyncMock(),
    ):
        result = await credit_service.register_credit_payment(
            _request(),
            order_id,
            Decimal("20.00"),
            "customer_wallet",
        )

    assert result["data"]["payment_method"] == "customer_wallet"
    apply_mock.assert_awaited_once()
    assert apply_mock.await_args.kwargs["profile_id"] == customer_id
    assert apply_mock.await_args.kwargs["amount_cop"] == Decimal("20.00")
    assert apply_mock.await_args.kwargs["order_id"] == order_id
    insert_args = conn.fetchrow.await_args_list[2].args
    assert insert_args[5] == "customer_wallet"


@pytest.mark.asyncio
async def test_register_credit_payment_wallet_insufficient_balance_skips_insert():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    customer_id = uuid4()
    group_id = uuid4()

    conn = MagicMock()
    conn.transaction.return_value = _AsyncContext(None)
    conn.fetchrow = AsyncMock(side_effect=[
        _credit_order(tenant_id, customer_id),
        {"id": group_id},
    ])
    conn.execute = AsyncMock()

    with patch(
        "app.services.credit_service.require_valid_session",
        return_value=_session(tenant_id, user_id),
    ), patch(
        "app.services.credit_service.get_db_connection",
        return_value=_AsyncContext(conn),
    ), patch(
        "app.services.customer_wallet_service.apply_wallet_for_order",
        new=AsyncMock(
            side_effect=APIError(
                "Saldo de billetera insuficiente. Disponible: 5.00, requerido: 20.00",
                status_code=400,
            )
        ),
    ):
        with pytest.raises(APIError) as exc_info:
            await credit_service.register_credit_payment(
                _request(),
                order_id,
                Decimal("20.00"),
                "customer_wallet",
            )

    assert exc_info.value.status_code == 400
    assert "insuficiente" in str(exc_info.value).lower() or "insuficiente" in (exc_info.value.message or "").lower()
    assert conn.execute.await_count == 0
    assert conn.fetchrow.await_count == 2


@pytest.mark.asyncio
async def test_register_credit_payment_wallet_requires_customer():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    group_id = uuid4()
    order = _credit_order(tenant_id, None)

    conn = MagicMock()
    conn.transaction.return_value = _AsyncContext(None)
    conn.fetchrow = AsyncMock(side_effect=[order, {"id": group_id}])
    conn.execute = AsyncMock()
    apply_mock = AsyncMock()

    with patch(
        "app.services.credit_service.require_valid_session",
        return_value=_session(tenant_id, user_id),
    ), patch(
        "app.services.credit_service.get_db_connection",
        return_value=_AsyncContext(conn),
    ), patch(
        "app.services.customer_wallet_service.apply_wallet_for_order",
        apply_mock,
    ):
        with pytest.raises(APIError) as exc_info:
            await credit_service.register_credit_payment(
                _request(),
                order_id,
                Decimal("20.00"),
                "customer_wallet",
            )

    assert exc_info.value.status_code == 400
    assert apply_mock.await_count == 0
    assert conn.execute.await_count == 0

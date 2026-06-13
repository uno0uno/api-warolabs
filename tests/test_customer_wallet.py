"""Tests for customer COP wallet service (api-warolabs#369)."""
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

from app.core.exceptions import APIError
from app.services.customer_wallet_service import (
    WALLET_PAYMENT_SLUG,
    _assert_tenant_customer,
    apply_wallet_for_order,
    assert_wallet_customer_identified,
    validate_wallet_payment_tender,
)


class TestWalletTenderValidation:
    def test_wallet_rejects_cash_received(self):
        with pytest.raises(APIError) as exc:
            validate_wallet_payment_tender(WALLET_PAYMENT_SLUG, cash_received=100.0)
        assert exc.value.status_code == 400

    def test_cash_allows_cash_received(self):
        validate_wallet_payment_tender("cash", cash_received=100.0)


class TestAnonymousGuard:
    @pytest.mark.asyncio
    async def test_anonymous_phone_rejected(self):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(
            return_value={"phone_number": "0000000000"},
        )
        with pytest.raises(APIError) as exc:
            await assert_wallet_customer_identified(conn, uuid4())
        assert "anónimo" in str(exc.value).lower() or "identificado" in str(exc.value)


class TestTenantCustomerGuard:
    @pytest.mark.asyncio
    async def test_uses_tenant_customer_relationship(self):
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=1)
        profile_id = uuid4()
        tenant_id = uuid4()
        await _assert_tenant_customer(conn, profile_id, tenant_id)
        query = conn.fetchval.await_args.args[0]
        assert "tenant_customers" in query
        assert "profile_id" in query
        assert "is_active = true" in query
        assert "tenant_members" not in query
        assert "role = 'customer'" not in query

    @pytest.mark.asyncio
    async def test_missing_association_raises_404(self):
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=None)
        with pytest.raises(APIError) as exc:
            await _assert_tenant_customer(conn, uuid4(), uuid4())
        assert exc.value.status_code == 404


class TestApplyWalletInsufficientBalance:
    @pytest.mark.asyncio
    async def test_insufficient_balance(self):
        conn = AsyncMock()

        async def fetchrow_side_effect(query, *args):
            q = " ".join(query.split())
            if "phone_number" in q:
                return {"phone_number": "3001234567"}
            if "FOR UPDATE" in q and "customer_wallet_balances" in q:
                return {"balance_cop": Decimal("10")}
            return None

        conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effect)
        with pytest.raises(APIError) as exc:
            await apply_wallet_for_order(
                conn,
                uuid4(),
                uuid4(),
                Decimal("50"),
                uuid4(),
                None,
            )
        assert "insuficiente" in str(exc.value).lower()

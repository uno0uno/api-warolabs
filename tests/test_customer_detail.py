"""Tests for GET /orders/customers/{customer_id} — profile without POS orders (#377)."""
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import Request

from app.core.exceptions import APIError
from app.services import orders_service

_TENANT_ID = UUID('93b3e582-34fa-44a6-8d0f-bf82a3608727')
_CUSTOMER_ID = uuid4()


class _SeqConnCtx:
    latest_conn = None

    def __init__(self, fetchrow_responses, fetch_responses=None):
        self._frows = iter(fetchrow_responses)
        self._fetch = iter(fetch_responses or [])

    async def __aenter__(self):
        conn = MagicMock()
        conn.fetchrow = AsyncMock(side_effect=lambda *a, **k: next(self._frows))
        conn.fetch = AsyncMock(side_effect=lambda *a, **k: next(self._fetch))
        _SeqConnCtx.latest_conn = conn
        return conn

    async def __aexit__(self, *_):
        return False


def _patch_session():
    fake = MagicMock()
    fake.tenant_id = _TENANT_ID
    return patch(
        'app.services.orders_service.require_valid_session',
        return_value=fake,
    )


def _patch_conn(fetchrow_responses, fetch_responses=None):
    return patch(
        'app.services.orders_service.get_db_connection',
        return_value=_SeqConnCtx(fetchrow_responses, fetch_responses),
    )


@pytest.mark.asyncio
async def test_get_customer_detail_profile_without_orders_returns_zeroed_stats():
    """Newly created tenant customer with no POS orders should return 200."""
    profile_row = {
        'customer_id': _CUSTOMER_ID,
        'name': 'Test Customer',
        'phone': '3001234567',
        'email': '3001234567@customer.temp',
    }

    with _patch_session(), _patch_conn([None, profile_row, None], [[], []]):
        result = await orders_service.get_customer_detail(
            Request({'type': 'http'}),
            customer_id=_CUSTOMER_ID,
        )

    assert result['success'] is True
    assert result['customer']['customer_id'] == str(_CUSTOMER_ID)
    assert result['customer']['total_orders'] == 0
    assert result['customer']['total_spent'] == 0.0
    assert result['customer']['first_purchase'] is None
    assert result['customer']['last_purchase'] is None
    assert result['orders']['items'] == []
    assert result['orders']['total'] == 0
    fallback_query = _SeqConnCtx.latest_conn.fetchrow.await_args_list[1].args[0]
    assert "tenant_customers tc" in fallback_query
    assert "tc.profile_id" in fallback_query
    assert "tc.is_active = true" in fallback_query
    assert "tenant_members" not in fallback_query
    assert "role = 'customer'" not in fallback_query


@pytest.mark.asyncio
async def test_get_customer_detail_unknown_profile_returns_404():
    with _patch_session(), _patch_conn([None, None]):
        with pytest.raises(APIError) as exc:
            await orders_service.get_customer_detail(
                Request({'type': 'http'}),
                customer_id=_CUSTOMER_ID,
            )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_customer_detail_with_orders_unchanged():
    """Existing aggregate path still returns order stats and dates."""
    order_agg = {
        'customer_id': _CUSTOMER_ID,
        'name': 'Returning Customer',
        'phone': '3009876543',
        'email': 'buyer@example.com',
        'total_orders': 2,
        'total_spent': 50000.0,
        'first_purchase': datetime(2026, 1, 10, tzinfo=timezone.utc),
        'last_purchase': datetime(2026, 5, 1, tzinfo=timezone.utc),
    }

    with _patch_session(), _patch_conn([order_agg, None], [[], []]):
        result = await orders_service.get_customer_detail(
            Request({'type': 'http'}),
            customer_id=_CUSTOMER_ID,
        )

    assert result['customer']['total_orders'] == 2
    assert result['customer']['total_spent'] == 50000.0
    assert result['customer']['first_purchase'] == '2026-01-10'
    assert result['customer']['last_purchase'] == '2026-05-01'
    aggregate_query = _SeqConnCtx.latest_conn.fetchrow.await_args_list[0].args[0]
    history_query = _SeqConnCtx.latest_conn.fetch.await_args_list[0].args[0]
    assert "o.online_cart_id IS NOT NULL" in aggregate_query
    assert "o.online_cart_id IS NOT NULL" in history_query


@pytest.mark.asyncio
async def test_get_customer_detail_aggregate_uses_tenant_local_date_filters():
    """Customer header stats should use the same tenant-local range as history."""
    order_agg = {
        'customer_id': _CUSTOMER_ID,
        'name': 'Filtered Customer',
        'phone': '3009876543',
        'email': 'buyer@example.com',
        'total_orders': 1,
        'total_spent': 25000.0,
        'first_purchase': date(2026, 6, 29),
        'last_purchase': date(2026, 6, 29),
    }

    with _patch_session(), _patch_conn([order_agg, None], [[], []]):
        result = await orders_service.get_customer_detail(
            Request({'type': 'http'}),
            customer_id=_CUSTOMER_ID,
            date_from='2026-06-29',
            date_to='2026-06-29',
        )

    aggregate_call = _SeqConnCtx.latest_conn.fetchrow.await_args_list[0]
    aggregate_query = aggregate_call.args[0]
    aggregate_args = aggregate_call.args[1:]
    assert "o.order_date >= ($4::timestamp AT TIME ZONE $5)" in aggregate_query
    assert "o.order_date < (($6::timestamp + interval '1 day') AT TIME ZONE $7)" in aggregate_query
    assert aggregate_args[3] == date(2026, 6, 29)
    assert aggregate_args[5] == date(2026, 6, 29)
    assert result['customer']['first_purchase'] == '2026-06-29'
    assert result['customer']['last_purchase'] == '2026-06-29'

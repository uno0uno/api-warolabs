"""Tests for GET /orders/customers list — include tenant customers without orders (#1099)."""
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import Request

from app.services import orders_service

_TENANT_ID = UUID('93b3e582-34fa-44a6-8d0f-bf82a3608727')
_CUSTOMER_WITH_ORDERS = uuid4()
_CUSTOMER_NO_ORDERS = uuid4()


class _ConnCtx:
    latest_conn = None

    def __init__(self, rows):
        self._rows = rows

    async def __aenter__(self):
        conn = MagicMock()
        conn.fetch = AsyncMock(return_value=self._rows)
        _ConnCtx.latest_conn = conn
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


def _patch_conn(rows):
    return patch(
        'app.services.orders_service.get_db_connection',
        return_value=_ConnCtx(rows),
    )


@pytest.mark.asyncio
async def test_customers_list_includes_zero_order_customer():
  rows = [
      {
          'customer_id': _CUSTOMER_WITH_ORDERS,
          'name': 'Buyer',
          'phone': '3001111111',
          'total_spent': 50000.0,
          'order_count': 2,
          'avg_ticket': 25000.0,
          'last_order_date': None,
          'total_count': 2,
          'total_revenue': 50000.0,
      },
      {
          'customer_id': _CUSTOMER_NO_ORDERS,
          'name': 'New Client',
          'phone': '3002222222',
          'total_spent': 0.0,
          'order_count': 0,
          'avg_ticket': 0.0,
          'last_order_date': None,
          'total_count': 2,
          'total_revenue': 50000.0,
      },
  ]

  with _patch_session(), _patch_conn(rows):
      result = await orders_service.get_customers_list(Request({'type': 'http'}))

  assert result['success'] is True
  assert result['total'] == 2
  assert len(result['data']) == 2
  zero_row = next(r for r in result['data'] if r['customer_id'] == str(_CUSTOMER_NO_ORDERS))
  assert zero_row['order_count'] == 0
  assert zero_row['total_spent'] == 0.0
  assert zero_row['last_order_date'] is None
  query = _ConnCtx.latest_conn.fetch.await_args.args[0]
  assert "tenant_customers tc" in query
  assert "tc.profile_id" in query
  assert "tc.is_active = true" in query
  assert "o.online_cart_id IS NOT NULL" in query
  assert "tenant_members" not in query
  assert "role = 'customer'" not in query
  assert "p.fiscal_id_type" in query
  assert "p.fiscal_id" in query
  assert "p.fiscal_business_name" in query
  assert "p.fiscal_email" in query
  for row in result['data']:
      assert 'fiscal_id_type' in row
      assert 'fiscal_id' in row
      assert 'fiscal_business_name' in row
      assert 'fiscal_email' in row


@pytest.mark.asyncio
async def test_customers_list_includes_fiscal_fields_when_present():
  rows = [
      {
          'customer_id': _CUSTOMER_WITH_ORDERS,
          'name': 'Buyer SA',
          'phone': '3001111111',
          'fiscal_id_type': 'NIT',
          'fiscal_id': '900123456',
          'fiscal_business_name': 'Buyer SA',
          'fiscal_email': 'factura@buyer.com',
          'total_spent': 50000.0,
          'order_count': 2,
          'avg_ticket': 25000.0,
          'last_order_date': None,
          'total_count': 1,
          'total_revenue': 50000.0,
      },
  ]

  with _patch_session(), _patch_conn(rows):
      result = await orders_service.get_customers_list(Request({'type': 'http'}))

  row = result['data'][0]
  assert row['fiscal_id_type'] == 'NIT'
  assert row['fiscal_id'] == '900123456'
  assert row['fiscal_business_name'] == 'Buyer SA'
  assert row['fiscal_email'] == 'factura@buyer.com'


@pytest.mark.asyncio
async def test_customers_list_fiscal_fields_null_when_absent():
  rows = [
      {
          'customer_id': _CUSTOMER_NO_ORDERS,
          'name': 'Walk-in',
          'phone': '3002222222',
          'fiscal_id_type': None,
          'fiscal_id': None,
          'fiscal_business_name': None,
          'fiscal_email': None,
          'total_spent': 0.0,
          'order_count': 0,
          'avg_ticket': 0.0,
          'last_order_date': None,
          'total_count': 1,
          'total_revenue': 0.0,
      },
  ]

  with _patch_session(), _patch_conn(rows):
      result = await orders_service.get_customers_list(Request({'type': 'http'}))

  row = result['data'][0]
  assert row['fiscal_id_type'] is None
  assert row['fiscal_id'] is None
  assert row['fiscal_business_name'] is None
  assert row['fiscal_email'] is None

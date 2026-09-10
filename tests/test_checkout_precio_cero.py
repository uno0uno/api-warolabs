from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.core.exceptions import APIError, ValidationError
from app.services.open_priced_service import resolve_line_unit_price


def _map(price="10.00", cortesia=False, open_priced=False):
    pid = uuid4()
    return str(pid), {
        str(pid): {"price": Decimal(price), "open_priced": open_priced, "es_cortesia": cortesia},
    }


def test_resolve_accepts_zero_for_courtesy_product():
    pid, pricing = _map(price="0.00", cortesia=True)
    assert resolve_line_unit_price(pricing, pid, "0") == 0


def test_resolve_rejects_zero_without_flag():
    pid, pricing = _map(price="10.00", cortesia=False)
    with pytest.raises(ValidationError):
        resolve_line_unit_price(pricing, pid, "0")


def test_resolve_rejects_zero_when_catalog_zero_but_no_flag():
    pid, pricing = _map(price="0.00", cortesia=False)
    with pytest.raises(ValidationError):
        resolve_line_unit_price(pricing, pid, "0")


def test_resolve_normal_price_still_matches():
    pid, pricing = _map(price="10.00", cortesia=False)
    assert resolve_line_unit_price(pricing, pid, "10.00") == 10


def test_validate_items_accepts_courtesy_lines():
    from app.services.open_priced_service import validate_items_unit_prices

    pid, pricing = _map(price="0.00", cortesia=True)
    items = [{"product_id": pid, "unit_price": "0", "modifiers": []}]
    validate_items_unit_prices(pricing, items)
    assert items[0]["unit_price"] == 0.0


@pytest.mark.asyncio
async def test_manual_order_rejects_zero_without_flag():
    from contextlib import asynccontextmanager

    from app.services import orders_service

    @asynccontextmanager
    async def _ctx():
        yield conn

    tenant_id = uuid4()
    pid = uuid4()
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[{"id": pid, "es_cortesia": False}])

    with (
        patch.object(orders_service, "require_valid_session", return_value=SimpleNamespace(user_id=uuid4(), tenant_id=tenant_id)),
        patch.object(orders_service, "get_db_connection", side_effect=lambda: _ctx()),
        patch.object(orders_service, "resolve_tenant_timezone", new=AsyncMock(return_value="America/Bogota")),
        patch.object(orders_service, "assert_order_not_in_closed_monthly_period", new=AsyncMock()),
        patch.object(orders_service, "resolve_modifier_selections", new=AsyncMock(return_value=[])),
    ):
        with pytest.raises(APIError):
            await orders_service.create_manual_order(
                object(),
                "2026-09-10T10:00",
                "cash",
                [{"product_id": str(pid), "quantity": 1, "unit_price": 0}],
            )

    query = conn.fetch.await_args.args[0]
    assert "es_cortesia" in query


@pytest.mark.asyncio
async def test_manual_order_accepts_zero_with_flag():
    from contextlib import asynccontextmanager

    from app.services import orders_service

    @asynccontextmanager
    async def _ctx():
        yield conn

    tenant_id = uuid4()
    pid = uuid4()
    conn = MagicMock()

    seen = {}

    async def _fetch(query, *args):
        if "es_cortesia" in query:
            seen["manual_check_ran"] = True
            return [{"id": pid, "es_cortesia": True}]
        return []

    conn.fetch = _fetch

    order_id = uuid4()

    async def _fetchrow(query, *args):
        from datetime import datetime

        if "INSERT INTO orders" in query:
            raise AssertionError("stop after courtesy gate")

        return {
            "id": order_id,
            "order_number": 1,
            "order_date": datetime(2026, 9, 10, 10, 0),
            "created_at": datetime(2026, 9, 10, 10, 0),
        }

    conn.fetchrow = _fetchrow

    with (
        patch.object(orders_service, "require_valid_session", return_value=SimpleNamespace(user_id=uuid4(), tenant_id=tenant_id)),
        patch.object(orders_service, "get_db_connection", side_effect=lambda: _ctx()),
        patch.object(orders_service, "resolve_tenant_timezone", new=AsyncMock(return_value="America/Bogota")),
        patch.object(orders_service, "assert_order_not_in_closed_monthly_period", new=AsyncMock()),
        patch.object(orders_service, "resolve_modifier_selections", new=AsyncMock(return_value=[])),
    ):
        from app.core.exceptions import APIError as ServiceAPIError

        with pytest.raises(ServiceAPIError) as exc:
            await orders_service.create_manual_order(
                object(),
                "2026-09-10T10:00",
                "cash",
                [{"product_id": str(pid), "quantity": 1, "unit_price": 0}],
            )

    assert "stop after courtesy gate" in str(exc.value)
    assert seen.get("manual_check_ran") is True

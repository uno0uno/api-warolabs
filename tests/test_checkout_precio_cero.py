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


def test_resolve_forces_zero_for_legacy_courtesy_with_price():
    pid, pricing = _map(price="15.00", cortesia=True)
    assert resolve_line_unit_price(pricing, pid, "0") == 0
    assert resolve_line_unit_price(pricing, pid, "15.00") == 0


@pytest.mark.asyncio
async def test_courtesy_rejects_priced_modifier():
    from app.services.modifier_option_service import resolve_modifier_selections

    pid, mid, gid = uuid4(), uuid4(), uuid4()
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=[
        [{"id": gid, "name": "Extras", "is_required": False, "min_qty": 0, "max_qty": 5}],
        [{"id": mid, "modifier_group_id": gid, "name": "Queso", "price": Decimal("500"),
          "max_limit": 3, "included_quantity": 0, "is_available": True}],
    ])
    with pytest.raises(APIError) as exc:
        await resolve_modifier_selections(
            conn, pid, [{"id": str(mid), "quantity": 1}], is_courtesy=True
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_courtesy_allows_free_modifier():
    from app.services.modifier_option_service import resolve_modifier_selections

    pid, mid, gid = uuid4(), uuid4(), uuid4()
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=[
        [{"id": gid, "name": "Extras", "is_required": False, "min_qty": 0, "max_qty": 5}],
        [{"id": mid, "modifier_group_id": gid, "name": "Sin cebolla", "price": Decimal("0"),
          "max_limit": 3, "included_quantity": 0, "is_available": True}],
    ])
    resolved = await resolve_modifier_selections(
        conn, pid, [{"id": str(mid), "quantity": 1}], is_courtesy=True
    )
    assert len(resolved) == 1


@pytest.mark.asyncio
async def test_non_courtesy_keeps_priced_modifier():
    from app.services.modifier_option_service import resolve_modifier_selections

    pid, mid, gid = uuid4(), uuid4(), uuid4()
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=[
        [{"id": gid, "name": "Extras", "is_required": False, "min_qty": 0, "max_qty": 5}],
        [{"id": mid, "modifier_group_id": gid, "name": "Queso", "price": Decimal("500"),
          "max_limit": 3, "included_quantity": 0, "is_available": True}],
    ])
    resolved = await resolve_modifier_selections(
        conn, pid, [{"id": str(mid), "quantity": 1}], is_courtesy=False
    )
    assert len(resolved) == 1


@pytest.mark.asyncio
async def test_online_cart_rejects_courtesy():
    from contextlib import asynccontextmanager

    from app.core.exceptions import APIError
    from app.services import online_cart_service

    pid = uuid4()
    conn = MagicMock()

    async def _fetch(query, *args):
        if "tenant_id != $2" in query:
            return []
        return [{"id": pid, "price": Decimal("0.00"), "es_cortesia": True}]

    conn.fetch = _fetch
    conn.fetchrow = AsyncMock(return_value={"id": uuid4()})

    @asynccontextmanager
    async def _tx():
        yield None

    conn.transaction.side_effect = lambda: _tx()

    @asynccontextmanager
    async def _ctx():
        yield conn

    with patch.object(online_cart_service, "get_db_connection", side_effect=lambda: _ctx()):
        with pytest.raises(APIError) as exc:
            await online_cart_service.create_cart_with_batch_items(
                uuid4(), [{"product_id": str(pid), "quantity": 1}]
            )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_table_qr_snapshot_rejects_courtesy():
    from fastapi import HTTPException

    from app.services import public_table_qr_service

    pid = uuid4()
    conn = MagicMock()
    conn.fetch = AsyncMock(
        return_value=[{"id": pid, "name": "Cafe", "price": Decimal("0.00"), "es_cortesia": True}]
    )
    with patch.object(
        public_table_qr_service, "validate_products_belong_to_tenant", new=AsyncMock()
    ):
        with pytest.raises(HTTPException) as exc:
            await public_table_qr_service._build_item_snapshots(
                conn, uuid4(), [{"product_id": str(pid), "quantity": 1}]
            )
    assert exc.value.status_code == 400


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
async def test_manual_order_zero_line_deducts_inventory_and_posts_cogs():
    from contextlib import asynccontextmanager
    from datetime import datetime

    from app.services import orders_service

    @asynccontextmanager
    async def _ctx():
        yield conn

    tenant_id = uuid4()
    pid = uuid4()
    ing_id = uuid4()
    conn = MagicMock()
    order_id = uuid4()
    order_dt = datetime(2026, 9, 10, 10, 0)

    async def _fetch(query, *args):
        if "es_cortesia" in query:
            return [{"id": pid, "es_cortesia": True}]
        if "FROM product_recipes" in query:
            return [{"ingredient_id": ing_id, "quantity": 3, "unit": "und", "ingredient_name": "Queso"}]
        return []

    conn.fetch = _fetch

    async def _fetchrow(query, *args):
        if "INSERT INTO orders" in query:
            return {"id": order_id, "order_number": 7, "order_date": order_dt, "created_at": order_dt}
        if "INSERT INTO order_items" in query:
            return {"id": uuid4()}
        if "SELECT current_stock" in query:
            return {"current_stock": 10}
        if "INSERT INTO order_payments" in query:
            return {"id": uuid4()}
        return {}

    conn.fetchrow = _fetchrow
    conn.execute = AsyncMock()

    @asynccontextmanager
    async def _tx():
        yield None

    conn.transaction.side_effect = lambda: _tx()

    with (
        patch.object(orders_service, "require_valid_session", return_value=SimpleNamespace(user_id=uuid4(), tenant_id=tenant_id)),
        patch.object(orders_service, "get_db_connection", side_effect=lambda: _ctx()),
        patch.object(orders_service, "resolve_tenant_timezone", new=AsyncMock(return_value="America/Bogota")),
        patch.object(orders_service, "assert_order_not_in_closed_monthly_period", new=AsyncMock()),
        patch.object(orders_service, "resolve_modifier_selections", new=AsyncMock(return_value=[])),
        patch.object(orders_service, "_pos_modifier_inventory_helpers", return_value=(AsyncMock(), None, None)),
        patch.object(orders_service, "_pos_order_item_ingredient_snapshot_helper", return_value=AsyncMock()),
        patch.object(orders_service, "_get_tenant_tax_config", new=AsyncMock(return_value={})),
        patch.object(orders_service, "_post_order_gl_entry", new=AsyncMock()),
        patch.object(orders_service, "_post_order_cogs_gl_entry", new=AsyncMock()) as mock_cogs,
        patch("app.services.credit_service.sync_order_split_credit_status", new=AsyncMock(return_value="settled")),
    ):
        result = await orders_service.create_manual_order(
            object(),
            "2026-09-10T10:00",
            "cash",
            [{"product_id": str(pid), "quantity": 2, "unit_price": 0}],
        )

    assert result["success"] is True
    assert result["data"]["total_amount"] == 0.0
    updates = [c.args for c in conn.execute.await_args_list if "UPDATE tenant_inventory" in c.args[0]]
    assert updates, "expected inventory UPDATE"
    assert float(updates[0][1]) == 4.0
    mock_cogs.assert_awaited_once()
    assert mock_cogs.await_args.kwargs["order_id"] == order_id

"""Resale Mi costo sync to warehouse seed when no purchases (#700)."""
from contextlib import asynccontextmanager
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import Request

from app.core.middleware import SessionContext
from app.models.product import ProductUpdate
from app.services import cost_resolution_service
from app.services.products_service import update_product_with_recipe


def _session():
    return SessionContext({
        "user_id": uuid4(),
        "tenant_id": uuid4(),
        "email": "test@warocol.com",
        "name": "Test",
        "expires_at": None,
        "is_active": True,
    })


@pytest.mark.asyncio
async def test_ingredient_has_purchase_unit_cost_true_and_false():
    conn = MagicMock()
    conn.fetchval = AsyncMock(side_effect=[True, False])
    tenant_id = uuid4()
    ingredient_id = uuid4()

    assert await cost_resolution_service.ingredient_has_purchase_unit_cost(
        conn, tenant_id=tenant_id, ingredient_id=ingredient_id,
    ) is True
    assert await cost_resolution_service.ingredient_has_purchase_unit_cost(
        conn, tenant_id=tenant_id, ingredient_id=ingredient_id,
    ) is False


@pytest.mark.asyncio
async def test_sync_resale_mi_costo_updates_ingredient_without_purchases():
    conn = MagicMock()
    ingredient_id = uuid4()
    product_id = uuid4()
    tenant_id = uuid4()
    conn.fetchval = AsyncMock(side_effect=[ingredient_id, False])
    conn.execute = AsyncMock()

    updated = await cost_resolution_service.sync_resale_mi_costo_to_ingredient(
        conn,
        tenant_id=tenant_id,
        product_id=product_id,
        costo_percibido=Decimal("28"),
    )

    assert updated is True
    conn.execute.assert_called_once()
    sql, ing_id, cost, tid = conn.execute.call_args[0]
    assert "UPDATE ingredients" in sql
    assert "costo_unitario" in sql
    assert ing_id == ingredient_id
    assert cost == 28.0
    assert tid == tenant_id


@pytest.mark.asyncio
async def test_sync_resale_mi_costo_skips_when_purchase_unit_cost_exists():
    conn = MagicMock()
    ingredient_id = uuid4()
    conn.fetchval = AsyncMock(side_effect=[ingredient_id, True])
    conn.execute = AsyncMock()

    updated = await cost_resolution_service.sync_resale_mi_costo_to_ingredient(
        conn,
        tenant_id=uuid4(),
        product_id=uuid4(),
        costo_percibido=Decimal("28"),
    )

    assert updated is False
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_update_resale_perceived_syncs_ingredient_then_recalcs():
    request = MagicMock(spec=Request)
    session = _session()
    product_id = uuid4()
    ingredient_id = uuid4()
    execute_sql = []

    async def _fetchrow(query, *args):
        if "FROM product WHERE id" in query and "is_resale" in query:
            return {"id": product_id, "name": "Club Colombia", "is_resale": True}
        return None

    async def _fetchval(query, *args):
        if "JOIN product_recipes" in query:
            return ingredient_id
        if "tenant_purchase_items" in query:
            return False
        return None

    async def _execute(query, *args):
        execute_sql.append(query)

    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    conn.fetchval = AsyncMock(side_effect=_fetchval)
    conn.execute = AsyncMock(side_effect=_execute)

    @asynccontextmanager
    async def _txn():
        yield

    conn.transaction.return_value = _txn()

    @asynccontextmanager
    async def _db_ctx():
        yield conn

    product_data = ProductUpdate(costo_percibido=Decimal("28"))
    mock_response = MagicMock()

    with patch("app.services.products_service.require_valid_session", return_value=session), \
         patch("app.services.products_service.get_db_connection", side_effect=_db_ctx), \
         patch("app.services.products_service.menu_history_service.get_product_snapshot", AsyncMock(return_value=None)), \
         patch("app.services.products_service.cost_resolution_service.product_has_any_recipe", AsyncMock(return_value=True)), \
         patch("app.services.products_service.cost_resolution_service.persist_product_costo_calculado", AsyncMock()) as mock_persist, \
         patch("app.services.products_service.get_product_by_id", AsyncMock(return_value=mock_response)):
        await update_product_with_recipe(request, product_id, product_data)

    assert any("costo_percibido" in q for q in execute_sql)
    assert any("UPDATE ingredients" in q and "costo_unitario" in q for q in execute_sql)
    mock_persist.assert_called_once()
    assert mock_persist.call_args.kwargs["tracks_inventory"] is True


@pytest.mark.asyncio
async def test_update_resale_perceived_does_not_clobber_purchase_seed():
    request = MagicMock(spec=Request)
    session = _session()
    product_id = uuid4()
    ingredient_id = uuid4()
    execute_sql = []

    async def _fetchrow(query, *args):
        if "FROM product WHERE id" in query and "is_resale" in query:
            return {"id": product_id, "name": "Club Colombia", "is_resale": True}
        return None

    async def _fetchval(query, *args):
        if "JOIN product_recipes" in query:
            return ingredient_id
        if "tenant_purchase_items" in query:
            return True
        return None

    async def _execute(query, *args):
        execute_sql.append(query)

    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    conn.fetchval = AsyncMock(side_effect=_fetchval)
    conn.execute = AsyncMock(side_effect=_execute)

    @asynccontextmanager
    async def _txn():
        yield

    conn.transaction.return_value = _txn()

    @asynccontextmanager
    async def _db_ctx():
        yield conn

    product_data = ProductUpdate(costo_percibido=Decimal("28"))

    with patch("app.services.products_service.require_valid_session", return_value=session), \
         patch("app.services.products_service.get_db_connection", side_effect=_db_ctx), \
         patch("app.services.products_service.menu_history_service.get_product_snapshot", AsyncMock(return_value=None)), \
         patch("app.services.products_service.cost_resolution_service.product_has_any_recipe", AsyncMock(return_value=True)), \
         patch("app.services.products_service.cost_resolution_service.persist_product_costo_calculado", AsyncMock()), \
         patch("app.services.products_service.get_product_by_id", AsyncMock(return_value=MagicMock())):
        await update_product_with_recipe(request, product_id, product_data)

    assert any("costo_percibido" in q for q in execute_sql)
    assert not any("UPDATE ingredients" in q for q in execute_sql)

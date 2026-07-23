"""Tests for costo_percibido (#745) — independent of costo_calculado."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import Request

from app.core.middleware import SessionContext
from app.models.product import ProductCreate, ProductUpdate
from app.services.products_service import create_product_with_recipe, update_product_with_recipe


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
async def test_create_product_with_perceived_only_no_recipe():
    """Perceived cost without recipe: costo_calculado stays NULL after persist."""
    request = MagicMock(spec=Request)
    session = _session()
    product_id = uuid4()
    persist_calls = []

    async def _fetchrow(query, *args):
        if "INSERT INTO product" in query:
            assert args[-1] == Decimal("8500")
            return {
                "id": product_id,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
        return None

    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    conn.execute = AsyncMock()

    async def _persist(pid, tid, c, *, tracks_inventory):
        persist_calls.append(tracks_inventory)

    @asynccontextmanager
    async def _txn():
        yield

    conn.transaction.return_value = _txn()

    @asynccontextmanager
    async def _db_ctx():
        yield conn

    product_data = ProductCreate(
        name="Servicio envío",
        price=Decimal("25000"),
        category_id=uuid4(),
        tenant_id=session.tenant_id,
        costo_percibido=Decimal("8500"),
        ingredients=[],
    )

    mock_response = MagicMock()

    with patch("app.services.products_service.require_valid_session", return_value=session), \
         patch("app.services.products_service.get_db_connection", side_effect=_db_ctx), \
         patch("app.services.products_service.cost_resolution_service.persist_product_costo_calculado", AsyncMock(side_effect=_persist)), \
         patch("app.services.products_service.menu_history_service.get_product_snapshot", AsyncMock(return_value=None)), \
         patch("app.services.products_service.get_product_by_id", AsyncMock(return_value=mock_response)):
        result = await create_product_with_recipe(request, product_data)

    assert result is mock_response
    assert persist_calls == [False]


@pytest.mark.asyncio
async def test_update_perceived_still_recalcs_real_not_perceived():
    """PATCH costo_percibido updates column; real cost recalc stays separate."""
    request = MagicMock(spec=Request)
    session = _session()
    product_id = uuid4()
    update_sql = []

    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"id": product_id, "name": "Test", "is_resale": False})
    conn.execute = AsyncMock(side_effect=lambda q, *a: update_sql.append(q))
    conn.fetchval = AsyncMock(return_value=None)
    @asynccontextmanager
    async def _txn():
        yield

    conn.transaction.return_value = _txn()

    @asynccontextmanager
    async def _db_ctx():
        yield conn

    product_data = ProductUpdate(costo_percibido=Decimal("9000"))

    mock_response = MagicMock()

    with patch("app.services.products_service.require_valid_session", return_value=session), \
         patch("app.services.products_service.get_db_connection", side_effect=_db_ctx), \
         patch("app.services.products_service.menu_history_service.get_product_snapshot", AsyncMock(return_value=None)), \
         patch("app.services.products_service.cost_resolution_service.product_has_any_recipe", AsyncMock(return_value=False)), \
         patch("app.services.products_service.cost_resolution_service.persist_product_costo_calculado", AsyncMock()) as mock_persist, \
         patch("app.services.products_service.get_product_by_id", AsyncMock(return_value=mock_response)):
        await update_product_with_recipe(request, product_id, product_data)

    assert any("costo_percibido" in q for q in update_sql)
    mock_persist.assert_called_once()
    assert mock_persist.call_args.kwargs["tracks_inventory"] is False


@pytest.mark.asyncio
async def test_recalculate_ingredient_only_updates_real_cost():
    """Purchase recalc path must not UPDATE costo_percibido."""
    from app.services import cost_resolution_service

    ingredient_id = uuid4()
    tenant_id = uuid4()
    product_id = uuid4()

    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[{"product_id": product_id}])
    persist = AsyncMock()

    with patch(
        "app.services.cost_resolution_service.persist_product_costo_calculado",
        persist,
    ):
        await cost_resolution_service.recalculate_products_for_ingredient(
            ingredient_id, tenant_id, conn
        )

    persist.assert_called_once()
    assert persist.call_args[0][0] == product_id
    assert "costo_percibido" not in str(persist.call_args)

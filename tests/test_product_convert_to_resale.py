"""Convert menu product without recipe to atomic resale."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import asyncpg
import pytest
from fastapi import HTTPException, Request
from pydantic import ValidationError

from app.core.middleware import SessionContext
from app.models.product import ProductConvertToResale
from app.services.products_service import convert_product_to_resale


def _session():
    return SessionContext({
        "user_id": uuid4(),
        "tenant_id": uuid4(),
        "email": "test@warocol.com",
        "name": "Test",
        "expires_at": None,
        "is_active": True,
    })


def _convert_body(**overrides):
    data = {
        "resale_unit_weight_gr": 350.0,
        "resale_unit_weight_unit": "ml",
    }
    data.update(overrides)
    return ProductConvertToResale(**data)


def test_convert_to_resale_requires_unit_weight():
    with pytest.raises(ValidationError):
        ProductConvertToResale(resale_unit_weight_gr=0)


@pytest.mark.asyncio
async def test_convert_product_to_resale_happy_path():
    request = MagicMock(spec=Request)
    session = _session()
    product_id = uuid4()
    ingredient_id = uuid4()
    category_id = uuid4()
    create_ingredient_calls = []
    persist_calls = []

    async def _fetchrow(query, *args):
        if "FROM product" in query and "WHERE id" in query:
            return {
                "id": product_id,
                "name": "Gaseosa 350ml",
                "category_id": category_id,
                "is_resale": False,
                "open_priced": False,
                "is_combo": False,
                "product_base_type_id": None,
                "costo_percibido": None,
            }
        if "SELECT name FROM categories" in query:
            return {"name": "Bebidas"}
        return None

    async def _fetchval(query, *args):
        if "product_modifier_groups" in query:
            return 0
        if "EXISTS" in query:
            return False
        return None

    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    conn.fetchval = AsyncMock(side_effect=_fetchval)
    conn.execute = AsyncMock()

    async def _persist(pid, tid, c, *, tracks_inventory):
        persist_calls.append(tracks_inventory)

    async def _create_ingredient(conn, tenant_id, data):
        create_ingredient_calls.append(data)
        return {"id": str(ingredient_id)}

    @asynccontextmanager
    async def _txn():
        yield

    conn.transaction.return_value = _txn()

    @asynccontextmanager
    async def _db_ctx():
        yield conn

    mock_response = MagicMock()
    mock_response.data = MagicMock()

    with patch("app.services.products_service.require_valid_session", return_value=session), \
         patch("app.services.products_service.get_db_connection", side_effect=_db_ctx), \
         patch("app.services.products_service.create_tenant_ingredient", AsyncMock(side_effect=_create_ingredient)), \
         patch("app.services.products_service.resolve_to_base_unit", AsyncMock(return_value=(Decimal("1"), "und"))), \
         patch("app.services.products_service.cost_resolution_service.persist_product_costo_calculado", AsyncMock(side_effect=_persist)), \
         patch("app.services.products_service.cost_resolution_service.product_has_any_recipe", AsyncMock(return_value=False)), \
         patch("app.services.products_service.menu_history_service.get_product_snapshot", AsyncMock(return_value=None)), \
         patch("app.services.products_service.get_product_by_id", AsyncMock(return_value=mock_response)):
        result = await convert_product_to_resale(request, product_id, _convert_body())

    assert result is mock_response
    assert result.data.resale_ingredient_id == ingredient_id
    assert len(create_ingredient_calls) == 1
    assert create_ingredient_calls[0].is_resale is True
    assert persist_calls == [True]


@pytest.mark.asyncio
async def test_convert_product_to_resale_rejects_already_resale():
    request = MagicMock(spec=Request)
    session = _session()
    product_id = uuid4()

    async def _fetchrow(query, *args):
        return {
            "id": product_id,
            "name": "Snack",
            "category_id": uuid4(),
            "is_resale": True,
            "open_priced": False,
            "is_combo": False,
            "product_base_type_id": None,
            "costo_percibido": None,
        }

    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=_fetchrow)

    @asynccontextmanager
    async def _db_ctx():
        yield conn

    with patch("app.services.products_service.require_valid_session", return_value=session), \
         patch("app.services.products_service.get_db_connection", side_effect=_db_ctx):
        with pytest.raises(HTTPException) as exc:
            await convert_product_to_resale(request, product_id, _convert_body())

    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_convert_product_to_resale_rejects_with_recipe():
    request = MagicMock(spec=Request)
    session = _session()
    product_id = uuid4()

    async def _fetchrow(query, *args):
        return {
            "id": product_id,
            "name": "Pizza",
            "category_id": uuid4(),
            "is_resale": False,
            "open_priced": False,
            "is_combo": False,
            "product_base_type_id": None,
            "costo_percibido": None,
        }

    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=_fetchrow)

    @asynccontextmanager
    async def _db_ctx():
        yield conn

    with patch("app.services.products_service.require_valid_session", return_value=session), \
         patch("app.services.products_service.get_db_connection", side_effect=_db_ctx), \
         patch("app.services.products_service.cost_resolution_service.product_has_any_recipe", AsyncMock(return_value=True)):
        with pytest.raises(HTTPException) as exc:
            await convert_product_to_resale(request, product_id, _convert_body())

    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_convert_product_to_resale_ingredient_duplicate_409():
    request = MagicMock(spec=Request)
    session = _session()
    product_id = uuid4()

    async def _fetchrow(query, *args):
        if "FROM product" in query:
            return {
                "id": product_id,
                "name": "Gaseosa",
                "category_id": uuid4(),
                "is_resale": False,
                "open_priced": False,
                "is_combo": False,
                "product_base_type_id": None,
                "costo_percibido": None,
            }
        if "SELECT name FROM categories" in query:
            return {"name": "Bebidas"}
        return None

    async def _fetchval(query, *args):
        if "product_modifier_groups" in query:
            return 0
        return False

    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    conn.fetchval = AsyncMock(side_effect=_fetchval)
    conn.execute = AsyncMock()
    conn.transaction = MagicMock()

    @asynccontextmanager
    async def _txn():
        yield

    conn.transaction.return_value = _txn()

    @asynccontextmanager
    async def _db_ctx():
        yield conn

    with patch("app.services.products_service.require_valid_session", return_value=session), \
         patch("app.services.products_service.get_db_connection", side_effect=_db_ctx), \
         patch("app.services.products_service.cost_resolution_service.product_has_any_recipe", AsyncMock(return_value=False)), \
         patch("app.services.products_service.menu_history_service.get_product_snapshot", AsyncMock(return_value=None)), \
         patch(
             "app.services.products_service.create_tenant_ingredient",
             AsyncMock(side_effect=HTTPException(status_code=409, detail="ingredient exists")),
         ):
        with pytest.raises(HTTPException) as exc:
            await convert_product_to_resale(request, product_id, _convert_body())

    assert exc.value.status_code == 409

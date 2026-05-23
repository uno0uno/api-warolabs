"""Atomic resale product + ingredient create (warocol.com#846)."""
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
from app.models.product import ProductCreate
from app.services.products_service import create_product_with_recipe


def _session():
    return SessionContext({
        "user_id": uuid4(),
        "tenant_id": uuid4(),
        "email": "test@warocol.com",
        "name": "Test",
        "expires_at": None,
        "is_active": True,
    })


def _base_product_create(**overrides):
    tenant_id = overrides.pop("tenant_id", uuid4())
    data = {
        "name": "Gaseosa 350ml",
        "price": Decimal("5000"),
        "category_id": uuid4(),
        "tenant_id": tenant_id,
        "is_resale": True,
        "auto_resale_ingredient": True,
        "resale_unit_weight_gr": 350.0,
        "resale_unit_weight_unit": "ml",
        "ingredients": [],
    }
    data.update(overrides)
    return ProductCreate(**data)


def test_product_create_auto_resale_requires_is_resale():
    with pytest.raises(ValidationError):
        _base_product_create(is_resale=False)


def test_product_create_auto_resale_rejects_ingredients():
    with pytest.raises(ValidationError):
        _base_product_create(
            ingredients=[{
                "ingredient_id": uuid4(),
                "quantity": 1,
                "unit": "und",
            }],
        )


def test_product_create_auto_resale_requires_unit_weight():
    with pytest.raises(ValidationError):
        _base_product_create(resale_unit_weight_gr=None)


@pytest.mark.asyncio
async def test_create_product_auto_resale_happy_path():
    request = MagicMock(spec=Request)
    session = _session()
    product_id = uuid4()
    ingredient_id = uuid4()
    persist_calls = []
    create_ingredient_calls = []

    async def _fetchrow(query, *args):
        if "INSERT INTO product" in query:
            return {
                "id": product_id,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
        if "SELECT name FROM categories" in query:
            return {"name": "Bebidas"}
        return None

    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
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

    product_data = _base_product_create(tenant_id=session.tenant_id)
    mock_response = MagicMock()
    mock_response.data = MagicMock()

    with patch("app.services.products_service.require_valid_session", return_value=session), \
         patch("app.services.products_service.get_db_connection", side_effect=_db_ctx), \
         patch("app.services.products_service.create_tenant_ingredient", AsyncMock(side_effect=_create_ingredient)), \
         patch("app.services.products_service.resolve_to_base_unit", AsyncMock(return_value=(Decimal("1"), "und"))), \
         patch("app.services.products_service.cost_resolution_service.persist_product_costo_calculado", AsyncMock(side_effect=_persist)), \
         patch("app.services.products_service.menu_history_service.get_product_snapshot", AsyncMock(return_value=None)), \
         patch("app.services.products_service.get_product_by_id", AsyncMock(return_value=mock_response)):
        result = await create_product_with_recipe(request, product_data)

    assert result is mock_response
    assert result.data.resale_ingredient_id == ingredient_id
    assert len(create_ingredient_calls) == 1
    assert create_ingredient_calls[0].unit == "und"
    assert create_ingredient_calls[0].is_resale is True
    assert create_ingredient_calls[0].unit_weight_gr == 350.0
    assert persist_calls == [True]
    recipe_inserts = [c for c in conn.execute.call_args_list if len(c[0]) >= 5]
    assert len(recipe_inserts) >= 1


@pytest.mark.asyncio
async def test_create_product_auto_resale_ingredient_duplicate_409():
    request = MagicMock(spec=Request)
    session = _session()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"name": "Bebidas"})
    conn.execute = AsyncMock()
    conn.transaction = MagicMock()

    @asynccontextmanager
    async def _txn():
        yield

    conn.transaction.return_value = _txn()

    @asynccontextmanager
    async def _db_ctx():
        yield conn

    product_data = _base_product_create(tenant_id=session.tenant_id)

    with patch("app.services.products_service.require_valid_session", return_value=session), \
         patch("app.services.products_service.get_db_connection", side_effect=_db_ctx), \
         patch(
             "app.services.products_service.create_tenant_ingredient",
             AsyncMock(side_effect=HTTPException(status_code=409, detail="ingredient exists")),
         ):
        with pytest.raises(HTTPException) as exc:
            await create_product_with_recipe(request, product_data)

    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_create_product_auto_resale_product_duplicate_rolls_back():
    request = MagicMock(spec=Request)
    session = _session()
    ingredient_id = uuid4()

    async def _fetchrow(query, *args):
        if "SELECT name FROM categories" in query:
            return {"name": "Bebidas"}
        if "INSERT INTO product" in query:
            raise asyncpg.UniqueViolationError("duplicate key")
        return None

    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    conn.execute = AsyncMock()
    conn.transaction = MagicMock()

    @asynccontextmanager
    async def _txn():
        yield

    conn.transaction.return_value = _txn()

    @asynccontextmanager
    async def _db_ctx():
        yield conn

    product_data = _base_product_create(tenant_id=session.tenant_id)

    with patch("app.services.products_service.require_valid_session", return_value=session), \
         patch("app.services.products_service.get_db_connection", side_effect=_db_ctx), \
         patch(
             "app.services.products_service.create_tenant_ingredient",
             AsyncMock(return_value={"id": str(ingredient_id)}),
         ):
        with pytest.raises(HTTPException) as exc:
            await create_product_with_recipe(request, product_data)

    assert exc.value.status_code == 409
    assert "producto" in exc.value.detail.lower()

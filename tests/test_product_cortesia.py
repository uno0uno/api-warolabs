from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.models.product import ProductCreate, ProductUpdate


def _base_create(**overrides):
    data = {
        "name": "Cortesia test",
        "price": "0",
        "es_cortesia": True,
        "category_id": uuid4(),
        "tenant_id": uuid4(),
    }
    data.update(overrides)
    if data.get("price") != "0" and "es_cortesia" not in overrides:
        data.pop("es_cortesia", None)
    return ProductCreate(**data)


def test_create_zero_price_requires_cortesia_flag():
    assert _base_create(es_cortesia=True).es_cortesia is True
    with pytest.raises(ValidationError):
        _base_create(es_cortesia=False)
    with pytest.raises(ValidationError):
        ProductCreate(name="X", price="0", category_id=uuid4(), tenant_id=uuid4())


def test_create_positive_price_without_flag():
    obj = _base_create(price="10.00")
    assert obj.es_cortesia is False


def test_update_zero_price_requires_explicit_flag():
    assert ProductUpdate(price="0", es_cortesia=True).es_cortesia is True
    with pytest.raises(ValidationError):
        ProductUpdate(price="0", es_cortesia=False)
    # None flag + zero price is allowed at model level (server checks DB row)
    assert ProductUpdate(price="0").es_cortesia is None


@pytest.mark.asyncio
async def test_create_inserts_es_cortesia():
    from contextlib import asynccontextmanager

    from app.services import products_service

    @asynccontextmanager
    async def _ctx():
        yield conn

    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"id": uuid4()})
    conn.fetchval = AsyncMock(return_value=False)
    conn.execute = AsyncMock()
    conn.transaction = MagicMock()

    @asynccontextmanager
    async def _tx():
        yield None

    conn.transaction.side_effect = lambda: _tx()

    payload = _base_create(tenant_id=tenant_id)

    with (
        patch.object(products_service, "require_valid_session", return_value=SimpleNamespace(user_id=uuid4(), tenant_id=tenant_id)),
        patch.object(products_service, "get_db_connection", side_effect=lambda: _ctx()),
        patch.object(products_service, "check_plan_quota_growth", new=AsyncMock()),
        patch.object(products_service, "_normalize_recipe_bases", return_value=[]),
    ):
        try:
            await products_service.create_product_with_recipe(object(), payload)
        except Exception:
            pass

    insert_sql = conn.fetchrow.await_args.args[0]
    assert "es_cortesia" in insert_sql

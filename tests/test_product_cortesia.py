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
    from datetime import datetime, timezone

    from app.services import products_service

    @asynccontextmanager
    async def _ctx():
        yield conn

    tenant_id = uuid4()
    table_id = uuid4()
    conn = MagicMock()
    full_row = {
        "id": table_id,
        "name": "Cortesia test",
        "description": None,
        "price": 0,
        "es_cortesia": True,
        "category_id": uuid4(),
        "category_name": None,
        "category_color": None,
        "preparation_time": None,
        "controla_stock": True,
        "is_available": True,
        "is_available_online": True,
        "is_available_table_qr": False,
        "is_combo": False,
        "is_resale": False,
        "open_priced": False,
        "allow_modifiers": True,
        "tax_category": "standard",
        "tax_resolution": "inherit",
        "tax_line_key": None,
        "costo_calculado": None,
        "costo_percibido": None,
        "precio_sugerido": None,
        "margen_objetivo": None,
        "tenant_id": tenant_id,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "station_id": None,
        "kitchen_name": None,
        "image_url": None,
        "ks_id": None,
        "station_name": None,
        "station_color": None,
        "margen_real_pct": None,
        "margen_real_valor": None,
        "margen_operativo_pct": None,
        "margen_operativo_valor": None,
        "margen_porcentaje": None,
        "margen_valor": None,
    }
    conn.fetchrow = AsyncMock(side_effect=[{"id": table_id}, full_row, full_row, full_row])
    conn.fetchval = AsyncMock(return_value=False)
    conn.execute = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])

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
        patch.object(products_service, "check_plan_quota_scoped", new=AsyncMock()),
    ):
        result = await products_service.create_product_with_recipe(object(), payload)

    insert_sql = conn.fetchrow.await_args_list[0].args[0]
    assert "es_cortesia" in insert_sql
    bound = conn.fetchrow.await_args_list[0].args[1:]
    assert True in bound
    assert result.success is True
    assert result.data.es_cortesia is True


@pytest.mark.asyncio
async def test_update_rejects_price_zero_without_flag():
    from contextlib import asynccontextmanager

    from app.core.exceptions import APIError
    from app.services import products_service

    @asynccontextmanager
    async def _ctx():
        yield conn

    tenant_id = uuid4()
    table_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"id": table_id, "name": "X", "is_resale": False})
    conn.fetchval = AsyncMock(return_value=False)
    conn.execute = AsyncMock()

    @asynccontextmanager
    async def _tx():
        yield None

    conn.transaction.side_effect = lambda: _tx()

    from app.models.product import ProductUpdate as PU

    with (
        patch.object(products_service, "require_valid_session", return_value=SimpleNamespace(user_id=uuid4(), tenant_id=tenant_id)),
        patch.object(products_service, "get_db_connection", side_effect=lambda: _ctx()),
        patch.object(products_service, "menu_history_service", new=SimpleNamespace(get_product_snapshot=AsyncMock(return_value={}))),
    ):
        with pytest.raises(APIError):
            await products_service.update_product_with_recipe(
                object(), table_id, PU(price="0")
            )


@pytest.mark.asyncio
async def test_update_rejects_clearing_flag_on_zero_price():
    from contextlib import asynccontextmanager

    from app.core.exceptions import APIError
    from app.services import products_service

    @asynccontextmanager
    async def _ctx():
        yield conn

    tenant_id = uuid4()
    table_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"id": table_id, "name": "X", "is_resale": False})
    conn.fetchval = AsyncMock(return_value=0)
    conn.execute = AsyncMock()

    @asynccontextmanager
    async def _tx():
        yield None

    conn.transaction.side_effect = lambda: _tx()

    from app.models.product import ProductUpdate as PU

    with (
        patch.object(products_service, "require_valid_session", return_value=SimpleNamespace(user_id=uuid4(), tenant_id=tenant_id)),
        patch.object(products_service, "get_db_connection", side_effect=lambda: _ctx()),
        patch.object(products_service, "menu_history_service", new=SimpleNamespace(get_product_snapshot=AsyncMock(return_value={}))),
    ):
        with pytest.raises(APIError):
            await products_service.update_product_with_recipe(
                object(), table_id, PU(es_cortesia=False)
            )

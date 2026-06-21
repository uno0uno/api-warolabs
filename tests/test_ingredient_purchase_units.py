"""Ingredient purchase-unit creation edge cases."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import Request

from app.core.middleware import SessionContext
from app.models.ingredient import IngredientPurchaseUnitCreate, IngredientPurchaseUnitUpdate
from app.routers.ingredient_purchase_units import router
from app.services.ingredient_purchase_units_service import (
    create_purchase_unit,
    resolve_recipe_quantity_to_base_unit,
    resolve_to_base_unit,
)


def _session():
    return SessionContext({
        "user_id": uuid4(),
        "tenant_id": uuid4(),
        "email": "test@warocol.com",
        "name": "Test",
        "expires_at": None,
        "is_active": True,
    })


def test_purchase_unit_models_keep_decimal_field_roles():
    ingredient_id = uuid4()

    data = IngredientPurchaseUnitCreate(
        ingredient_id=ingredient_id,
        purchase_unit="paquete",
        purchase_unit_label="Paquete precision",
        conversion_factor="1.345678",
        unit_cost="250.123456",
    )
    update = IngredientPurchaseUnitUpdate(
        conversion_factor="0.333333",
        unit_cost="6.617100",
    )

    assert data.conversion_factor == Decimal("1.345678")
    assert data.unit_cost == Decimal("250.123456")
    assert update.conversion_factor == Decimal("0.333333")
    assert update.unit_cost == Decimal("6.617100")


@pytest.mark.asyncio
async def test_create_purchase_unit_is_idempotent_for_existing_unit():
    request = MagicMock(spec=Request)
    response = MagicMock()
    session = _session()
    ingredient_id = uuid4()
    purchase_unit_id = uuid4()
    queries = []

    async def _fetchrow(query, *args):
        queries.append(query)
        if "SELECT id FROM ingredients" in query:
            return {"id": ingredient_id}
        return {
            "id": purchase_unit_id,
            "ingredient_id": ingredient_id,
            "purchase_unit": "und",
            "purchase_unit_label": "Unidad",
            "conversion_factor": 1.0,
            "unit_cost": None,
            "is_default": True,
            "is_active": True,
            "notes": None,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }

    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=_fetchrow)

    @asynccontextmanager
    async def _db_ctx():
        yield conn

    data = IngredientPurchaseUnitCreate(
        ingredient_id=ingredient_id,
        purchase_unit="und",
        purchase_unit_label="Unidad",
        conversion_factor=1,
        is_default=True,
        is_active=True,
    )

    with patch("app.services.ingredient_purchase_units_service.require_valid_session", return_value=session), \
         patch("app.services.ingredient_purchase_units_service.get_db_connection", side_effect=_db_ctx):
        result = await create_purchase_unit(request, response, data)

    insert_query = next(query for query in queries if "INSERT INTO ingredient_purchase_units" in query)
    assert "ON CONFLICT (ingredient_id, purchase_unit, purchase_unit_label)" in insert_query
    assert result.success is True
    assert result.data.id == purchase_unit_id


def test_purchase_unit_router_accepts_trailing_slash_post():
    slash_routes = [
        route for route in router.routes
        if getattr(route, "path", None) == "/" and "POST" in getattr(route, "methods", set())
    ]

    assert slash_routes
    assert slash_routes[0].include_in_schema is False


@pytest.mark.asyncio
async def test_resolve_to_base_unit_preserves_six_decimal_conversion():
    ingredient_id = uuid4()
    conn = MagicMock()

    async def _fetchrow(query, *args):
        if "SELECT unit FROM ingredients" in query:
            return {"unit": "gr"}
        return {"conversion_factor": Decimal("1.345678")}

    conn.fetchrow = AsyncMock(side_effect=_fetchrow)

    base_qty, base_unit = await resolve_to_base_unit(
        conn,
        ingredient_id,
        Decimal("2.000000"),
        "paquete",
    )

    assert base_qty == Decimal("2.691356")
    assert base_unit == "gr"


@pytest.mark.asyncio
async def test_resolve_to_base_unit_rounds_to_six_without_float_tail():
    ingredient_id = uuid4()
    conn = MagicMock()

    async def _fetchrow(query, *args):
        if "SELECT unit FROM ingredients" in query:
            return {"unit": "gr"}
        return {"conversion_factor": Decimal("0.333333")}

    conn.fetchrow = AsyncMock(side_effect=_fetchrow)

    base_qty, base_unit = await resolve_to_base_unit(
        conn,
        ingredient_id,
        Decimal("3"),
        "porcion",
    )

    assert base_qty == Decimal("0.999999")
    assert base_unit == "gr"


@pytest.mark.asyncio
async def test_resolve_recipe_quantity_to_base_unit_preserves_fractional_units():
    ingredient_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"unit": "und", "unit_weight_gr": Decimal("1.345")})

    converted = await resolve_recipe_quantity_to_base_unit(
        conn,
        ingredient_id,
        Decimal("8900"),
        "gr",
    )

    assert converted == 6617.100372

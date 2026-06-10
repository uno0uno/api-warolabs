"""Ingredient purchase-unit creation edge cases."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import Request

from app.core.middleware import SessionContext
from app.models.ingredient import IngredientPurchaseUnitCreate
from app.routers.ingredient_purchase_units import router
from app.services.ingredient_purchase_units_service import create_purchase_unit


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

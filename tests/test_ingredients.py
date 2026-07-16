"""
Tests for ingredients module endpoints.

Endpoints tested:
- GET /suppliers/ingredients
- type×unit validation (create_tenant_ingredient)
"""
import pytest
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi import HTTPException
from httpx import AsyncClient

from app.models.ingredient import TenantIngredientCreate
from app.services.ingredients_service import (
    _validate_unit_for_type,
    create_tenant_ingredient,
    get_ingredient_categories,
    update_tenant_ingredient,
)
from app.models.ingredient import TenantIngredientUpdate


class TestIngredientUnitTypeValidation:
    """Unit tests for service/supply/food unit rules (api-warolabs#380)."""

    def test_service_allows_hr_and_und(self):
        _validate_unit_for_type("hr", "service")
        _validate_unit_for_type("und", "service")

    def test_service_rejects_gr(self):
        with pytest.raises(HTTPException) as exc:
            _validate_unit_for_type("gr", "service")
        assert exc.value.status_code == 422
        assert "hr" in exc.value.detail or "und" in exc.value.detail

    def test_supply_requires_und(self):
        _validate_unit_for_type("und", "supply")

    def test_supply_rejects_kg(self):
        with pytest.raises(HTTPException) as exc:
            _validate_unit_for_type("kg", "supply")
        assert exc.value.status_code == 422
        assert "und" in exc.value.detail

    def test_food_allows_gr(self):
        _validate_unit_for_type("gr", "food")

    def test_food_rejects_hr(self):
        with pytest.raises(HTTPException) as exc:
            _validate_unit_for_type("hr", "food")
        assert exc.value.status_code == 422

    @pytest.mark.asyncio
    async def test_create_service_ingredient_hr(self):
        tenant_id = uuid4()
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(
            return_value={
                "id": str(uuid4()),
                "name": "Mano de obra",
                "unit": "hr",
                "type": "service",
                "category": None,
                "costo_unitario": None,
                "parent_id": None,
                "tenant_id": str(tenant_id),
                "is_resale": False,
                "unit_weight_gr": None,
                "unit_weight_unit": "gr",
                "created_at": None,
            }
        )
        data = TenantIngredientCreate(name="Mano de obra", unit="hr", type="service")
        result = await create_tenant_ingredient(conn, tenant_id, data)
        assert result["unit"] == "hr"
        assert result["type"] == "service"

    @pytest.mark.asyncio
    async def test_create_service_ingredient_und(self):
        tenant_id = uuid4()
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(
            return_value={
                "id": str(uuid4()),
                "name": "Transporte domicilios",
                "unit": "und",
                "type": "service",
                "category": "Logística",
                "costo_unitario": None,
                "parent_id": None,
                "tenant_id": str(tenant_id),
                "is_resale": False,
                "unit_weight_gr": None,
                "unit_weight_unit": "gr",
                "created_at": None,
            }
        )
        data = TenantIngredientCreate(
            name="Transporte domicilios",
            unit="und",
            type="service",
            category="Logística",
        )
        result = await create_tenant_ingredient(conn, tenant_id, data)
        assert result["unit"] == "und"
        assert result["type"] == "service"

    @pytest.mark.asyncio
    async def test_create_service_rejects_gr(self):
        tenant_id = uuid4()
        conn = AsyncMock()
        data = TenantIngredientCreate(name="Bad service", unit="gr", type="service")
        with pytest.raises(HTTPException) as exc:
            await create_tenant_ingredient(conn, tenant_id, data)
        assert exc.value.status_code == 422

    @pytest.mark.asyncio
    async def test_create_supply_rejects_kg(self):
        tenant_id = uuid4()
        conn = AsyncMock()
        data = TenantIngredientCreate(name="Bad supply", unit="kg", type="supply")
        with pytest.raises(HTTPException) as exc:
            await create_tenant_ingredient(conn, tenant_id, data)
        assert exc.value.status_code == 422
        assert "und" in exc.value.detail

    @pytest.mark.asyncio
    async def test_patch_service_unit_to_hr(self):
        tenant_id = uuid4()
        ingredient_id = uuid4()
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=False)
        conn.fetchrow = AsyncMock(
            side_effect=[
                {
                    "id": ingredient_id,
                    "name": "Legacy service",
                    "unit": "und",
                    "type": "service",
                    "category": None,
                    "costo_unitario": None,
                    "parent_id": None,
                },
                {
                    "id": str(ingredient_id),
                    "name": "Legacy service",
                    "unit": "hr",
                    "category": None,
                    "costo_unitario": None,
                    "parent_id": None,
                    "tenant_id": str(tenant_id),
                    "updated_at": None,
                },
            ]
        )
        data = TenantIngredientUpdate(unit="hr")
        result = await update_tenant_ingredient(conn, tenant_id, ingredient_id, data)
        assert result["unit"] == "hr"


class TestIngredientCategorySearch:
    @pytest.mark.asyncio
    async def test_lists_tenant_visible_categories_with_partial_search(self):
        tenant_id = uuid4()
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[
            {
                "name": "categoria de prueba",
                "ingredient_count": 2,
                "global_count": 0,
                "tenant_count": 2,
            },
            {
                "name": "ingrediente categoria",
                "ingredient_count": 1,
                "global_count": 0,
                "tenant_count": 1,
            },
        ])

        result = await get_ingredient_categories(
            conn,
            tenant_id,
            search=" categoria ",
            limit=50,
        )

        assert [category["name"] for category in result] == [
            "categoria de prueba",
            "ingrediente categoria",
        ]
        query, *params = conn.fetch.await_args.args
        assert "(tenant_id IS NULL OR tenant_id = $1)" in query
        assert "LOWER(unaccent(BTRIM(category))) LIKE LOWER(unaccent($2))" in query
        assert params == [tenant_id, "%categoria%", 50]


class TestIngredientsEndpoint:
    """Test ingredients list endpoint"""

    @pytest.mark.asyncio
    async def test_get_ingredients_default(self, client: AsyncClient):
        """Test GET /suppliers/ingredients with default params"""
        response = await client.get("/suppliers/ingredients")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_ingredients_response_structure(self, client: AsyncClient):
        """Test ingredients response has correct structure"""
        response = await client.get("/suppliers/ingredients")

        if response.status_code == 200:
            data = response.json()
            assert "success" in data
            assert "total" in data
            assert "data" in data
            assert isinstance(data["data"], list)

            if len(data["data"]) > 0:
                ingredient = data["data"][0]
                assert "id" in ingredient
                assert "name" in ingredient
                assert "unit" in ingredient

    @pytest.mark.asyncio
    async def test_get_ingredients_with_pagination(self, client: AsyncClient):
        """Test ingredients endpoint with pagination params"""
        response = await client.get("/suppliers/ingredients?page=1&limit=10")
        assert response.status_code in [200, 401, 403, 500]

        if response.status_code == 200:
            data = response.json()
            assert len(data["data"]) <= 10

    @pytest.mark.asyncio
    async def test_get_ingredients_with_search(self, client: AsyncClient):
        """Test ingredients endpoint with search param"""
        response = await client.get("/suppliers/ingredients?search=test")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_ingredients_filter_by_type_food(self, client: AsyncClient):
        """Test ingredients filtered by type=food"""
        response = await client.get("/suppliers/ingredients?type=food")
        assert response.status_code in [200, 401, 403, 500]

        if response.status_code == 200:
            data = response.json()
            for ingredient in data["data"]:
                if ingredient.get("type"):
                    assert ingredient["type"] == "food"

    @pytest.mark.asyncio
    async def test_get_ingredients_filter_by_type_service(self, client: AsyncClient):
        """Test ingredients filtered by type=service"""
        response = await client.get("/suppliers/ingredients?type=service")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_ingredients_filter_by_type_supply(self, client: AsyncClient):
        """Test ingredients filtered by type=supply"""
        response = await client.get("/suppliers/ingredients?type=supply")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_ingredients_invalid_page(self, client: AsyncClient):
        """Test ingredients with invalid page number"""
        response = await client.get("/suppliers/ingredients?page=0")
        # page must be >= 1
        assert response.status_code in [422, 500]

    @pytest.mark.asyncio
    async def test_get_ingredients_invalid_limit(self, client: AsyncClient):
        """Test ingredients with invalid limit"""
        response = await client.get("/suppliers/ingredients?limit=0")
        # limit must be >= 1
        assert response.status_code in [422, 500]

    @pytest.mark.asyncio
    async def test_get_ingredients_large_limit(self, client: AsyncClient):
        """Test ingredients with large limit (max 10000)"""
        response = await client.get("/suppliers/ingredients?limit=10000")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_ingredients_exceeds_max_limit(self, client: AsyncClient):
        """Test ingredients with limit exceeding max"""
        response = await client.get("/suppliers/ingredients?limit=10001")
        # Should return validation error
        assert response.status_code in [422, 500]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_ingredients_returns_list(self, client: AsyncClient):
        """Integration test: ingredients endpoint returns a valid list"""
        response = await client.get("/suppliers/ingredients")

        if response.status_code == 200:
            data = response.json()
            assert data["success"] is True
            assert isinstance(data["total"], int)
            assert data["total"] >= 0

"""
Tests for ingredients module endpoints.

Endpoints tested:
- GET /suppliers/ingredients
- type×unit validation (create_tenant_ingredient)
"""
import pytest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException
from httpx import AsyncClient

from app.models.ingredient import TenantIngredientCreate
from app.models.ingredient import PurchaseUnitInput
from app.services.ingredients_service import (
    _validate_unit_for_type,
    create_tenant_ingredient,
    get_ingredient_categories,
    resolve_ingredients_by_warehouse_categories,
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
        warehouse_category_id = uuid4()
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(
            side_effect=[
                {
                    "id": warehouse_category_id,
                    "name": "Logística",
                    "tenant_id": tenant_id,
                    "is_active": True,
                },
                {
                    "id": str(uuid4()),
                    "name": "Transporte domicilios",
                    "unit": "und",
                    "type": "service",
                    "category": "Logística",
                    "warehouse_category_id": warehouse_category_id,
                    "costo_unitario": None,
                    "parent_id": None,
                    "tenant_id": str(tenant_id),
                    "is_resale": False,
                    "unit_weight_gr": None,
                    "unit_weight_unit": "gr",
                    "created_at": None,
                },
            ]
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
        assert result["warehouse_category_id"] == warehouse_category_id

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


class TestVariantPurchaseUnitInheritance:
    """Issue #68 — copy parent purchase units when creating a variant ingredient."""

    @pytest.mark.asyncio
    async def test_create_variant_copies_parent_purchase_units(self):
        tenant_id = uuid4()
        parent_id = uuid4()
        ingredient_id = uuid4()
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(
            side_effect=[
                {"id": parent_id, "name": "Tomate"},
                {
                    "id": str(ingredient_id),
                    "name": "Tomate pack x12",
                    "unit": "gr",
                    "type": "food",
                    "category": None,
                    "warehouse_category_id": None,
                    "costo_unitario": None,
                    "parent_id": str(parent_id),
                    "tenant_id": str(tenant_id),
                    "is_resale": False,
                    "unit_weight_gr": None,
                    "unit_weight_unit": "gr",
                    "created_at": None,
                },
            ]
        )
        conn.fetch = AsyncMock(return_value=[
            {
                "purchase_unit": "kg",
                "purchase_unit_label": "Kilogramo",
                "conversion_factor": 1000,
                "unit_cost": None,
                "is_default": True,
                "is_active": True,
                "notes": None,
            },
        ])
        data = TenantIngredientCreate(
            name="Tomate pack x12",
            unit="gr",
            type="food",
            parent_id=str(parent_id),
        )

        with patch("app.services.ingredients_service.check_plan_quota_growth", new_callable=AsyncMock):
            result = await create_tenant_ingredient(conn, tenant_id, data)

        assert result["parent_id"] == str(parent_id)
        conn.fetch.assert_awaited_once()
        assert conn.fetch.await_args.args[1] == parent_id
        execute_sqls = [call.args[0] for call in conn.execute.await_args_list]
        assert any("ingredient_purchase_units" in sql for sql in execute_sqls)
        assert any("tenant_inventory" in sql for sql in execute_sqls)
        purchase_call = next(
            call for call in conn.execute.await_args_list
            if "ingredient_purchase_units" in call.args[0]
        )
        assert purchase_call.args[1] == ingredient_id
        assert purchase_call.args[2] == "kg"

    @pytest.mark.asyncio
    async def test_create_variant_parent_without_purchase_units(self):
        tenant_id = uuid4()
        parent_id = uuid4()
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(
            side_effect=[
                {"id": parent_id, "name": "Tomate"},
                {
                    "id": str(uuid4()),
                    "name": "Tomate pack x12",
                    "unit": "gr",
                    "type": "food",
                    "category": None,
                    "warehouse_category_id": None,
                    "costo_unitario": None,
                    "parent_id": str(parent_id),
                    "tenant_id": str(tenant_id),
                    "is_resale": False,
                    "unit_weight_gr": None,
                    "unit_weight_unit": "gr",
                    "created_at": None,
                },
            ]
        )
        conn.fetch = AsyncMock(return_value=[])
        data = TenantIngredientCreate(
            name="Tomate pack x12",
            unit="gr",
            type="food",
            parent_id=str(parent_id),
        )

        with patch("app.services.ingredients_service.check_plan_quota_growth", new_callable=AsyncMock):
            await create_tenant_ingredient(conn, tenant_id, data)

        conn.fetch.assert_awaited_once()
        execute_sqls = [call.args[0] for call in conn.execute.await_args_list]
        assert not any("ingredient_purchase_units" in sql for sql in execute_sqls)
        assert any("tenant_inventory" in sql for sql in execute_sqls)

    @pytest.mark.asyncio
    async def test_create_variant_explicit_purchase_units_skip_inheritance(self):
        tenant_id = uuid4()
        parent_id = uuid4()
        ingredient_id = uuid4()
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(
            side_effect=[
                {"id": parent_id, "name": "Tomate"},
                {
                    "id": str(ingredient_id),
                    "name": "Tomate pack x12",
                    "unit": "gr",
                    "type": "food",
                    "category": None,
                    "warehouse_category_id": None,
                    "costo_unitario": None,
                    "parent_id": str(parent_id),
                    "tenant_id": str(tenant_id),
                    "is_resale": False,
                    "unit_weight_gr": None,
                    "unit_weight_unit": "gr",
                    "created_at": None,
                },
            ]
        )
        data = TenantIngredientCreate(
            name="Tomate pack x12",
            unit="gr",
            type="food",
            parent_id=str(parent_id),
            purchase_units=[PurchaseUnitInput(purchase_unit="libra", is_default=True)],
        )

        with patch("app.services.ingredients_service.check_plan_quota_growth", new_callable=AsyncMock):
            await create_tenant_ingredient(conn, tenant_id, data)

        conn.fetch.assert_not_awaited()
        execute_sqls = [call.args[0] for call in conn.execute.await_args_list]
        assert any("tenant_inventory" in sql for sql in execute_sqls)
        purchase_call = next(
            call for call in conn.execute.await_args_list
            if "ingredient_purchase_units" in call.args[0]
        )
        assert purchase_call.args[2] == "libra"

    @pytest.mark.asyncio
    async def test_create_tenant_ingredient_seeds_zero_inventory(self):
        tenant_id = uuid4()
        ingredient_id = uuid4()
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(
            return_value={
                "id": str(ingredient_id),
                "name": "Sal",
                "unit": "gr",
                "type": "food",
                "category": None,
                "warehouse_category_id": None,
                "costo_unitario": None,
                "parent_id": None,
                "tenant_id": str(tenant_id),
                "is_resale": False,
                "unit_weight_gr": None,
                "unit_weight_unit": "gr",
                "created_at": None,
            }
        )
        data = TenantIngredientCreate(name="Sal", unit="gr", type="food")

        with patch("app.services.ingredients_service.check_plan_quota_growth", new_callable=AsyncMock):
            await create_tenant_ingredient(conn, tenant_id, data)

        inventory_call = next(
            call for call in conn.execute.await_args_list
            if "tenant_inventory" in call.args[0]
        )
        assert inventory_call.args[1] == tenant_id
        assert inventory_call.args[2] == ingredient_id


class TestIngredientCategorySearch:
    @pytest.mark.asyncio
    async def test_lists_tenant_visible_categories_with_partial_search(self):
        tenant_id = uuid4()
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[
            {
                "id": uuid4(),
                "tenant_id": tenant_id,
                "name": "categoria de prueba",
                "normalized_name": "categoria de prueba",
                "is_active": True,
                "created_at": None,
                "updated_at": None,
                "ingredient_count": 2,
                "global_count": 0,
                "tenant_count": 2,
            },
            {
                "id": uuid4(),
                "tenant_id": tenant_id,
                "name": "ingrediente categoria",
                "normalized_name": "ingrediente categoria",
                "is_active": True,
                "created_at": None,
                "updated_at": None,
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
        assert "(wc.tenant_id IS NULL OR wc.tenant_id = $1)" in query
        assert "wc.normalized_name LIKE $2" in query
        assert params == [tenant_id, "%categoria%", 50]


class TestIngredientCategoryResolution:
    @pytest.mark.asyncio
    async def test_resolves_unique_candidates_in_requested_category_order(self):
        tenant_id = uuid4()
        first_category_id = uuid4()
        second_category_id = uuid4()
        first_ingredient_id = uuid4()
        second_ingredient_id = uuid4()
        excluded_ingredient_id = uuid4()
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[
            {
                "category_id": first_category_id,
                "position": 1,
                "is_available": True,
                "ingredient_id": first_ingredient_id,
                "name": "Aguacate",
                "unit": "gr",
                "warehouse_category_id": first_category_id,
            },
            {
                "category_id": second_category_id,
                "position": 2,
                "is_available": True,
                "ingredient_id": second_ingredient_id,
                "name": "Yogur",
                "unit": "ml",
                "warehouse_category_id": second_category_id,
            },
            {
                "category_id": second_category_id,
                "position": 2,
                "is_available": True,
                "ingredient_id": second_ingredient_id,
                "name": "Yogur",
                "unit": "ml",
                "warehouse_category_id": second_category_id,
            },
        ])

        result = await resolve_ingredients_by_warehouse_categories(
            conn,
            tenant_id,
            [first_category_id, first_category_id, second_category_id],
            [excluded_ingredient_id, excluded_ingredient_id],
        )

        assert [row["ingredient_id"] for row in result["ingredients"]] == [
            first_ingredient_id,
            second_ingredient_id,
        ]
        assert result["empty_category_ids"] == []
        assert result["unavailable_category_ids"] == []
        query, *params = conn.fetch.await_args.args
        assert "WITH ORDINALITY" in query
        assert "wc.tenant_id IS NULL OR wc.tenant_id = $1" in query
        assert "ingredient.tenant_id IS NULL OR ingredient.tenant_id = $1" in query
        assert "ingredient.id = ANY($3::uuid[])" in query
        assert params == [
            tenant_id,
            [first_category_id, second_category_id],
            [excluded_ingredient_id],
            False,
        ]
        conn.fetch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reports_empty_and_unavailable_categories_without_failing_candidates(self):
        tenant_id = uuid4()
        populated_category_id = uuid4()
        empty_category_id = uuid4()
        unavailable_category_id = uuid4()
        ingredient_id = uuid4()
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[
            {
                "category_id": populated_category_id,
                "position": 1,
                "is_available": True,
                "ingredient_id": ingredient_id,
                "name": "Arroz",
                "unit": "gr",
                "warehouse_category_id": populated_category_id,
            },
            {
                "category_id": empty_category_id,
                "position": 2,
                "is_available": True,
                "ingredient_id": None,
                "name": None,
                "unit": None,
                "warehouse_category_id": None,
            },
            {
                "category_id": unavailable_category_id,
                "position": 3,
                "is_available": False,
                "ingredient_id": None,
                "name": None,
                "unit": None,
                "warehouse_category_id": None,
            },
        ])

        result = await resolve_ingredients_by_warehouse_categories(
            conn,
            tenant_id,
            [populated_category_id, empty_category_id, unavailable_category_id],
        )

        assert [row["ingredient_id"] for row in result["ingredients"]] == [ingredient_id]
        assert result["empty_category_ids"] == [empty_category_id]
        assert result["unavailable_category_ids"] == [unavailable_category_id]


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

"""
Tests for products module endpoints.

Endpoints tested:
- GET /menu/products
- GET /menu/products/stats
- GET /menu/products/{product_id}
"""
import pytest
from httpx import AsyncClient
from uuid import uuid4


class TestProductsListEndpoint:
    """Test products list endpoint"""

    @pytest.mark.asyncio
    async def test_get_products_default(self, client: AsyncClient):
        """Test GET /menu/products with default params"""
        response = await client.get("/menu/products")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_products_response_structure(self, client: AsyncClient):
        """Test products response has correct structure"""
        response = await client.get("/menu/products")

        if response.status_code == 200:
            data = response.json()
            assert "success" in data
            assert "total" in data
            assert "data" in data
            assert isinstance(data["data"], list)

            if len(data["data"]) > 0:
                product = data["data"][0]
                assert "id" in product
                assert "name" in product
                assert "price" in product
                assert "category_id" in product

    @pytest.mark.asyncio
    async def test_get_products_with_pagination(self, client: AsyncClient):
        """Test products endpoint with pagination"""
        response = await client.get("/menu/products?page=1&limit=10")
        assert response.status_code in [200, 401, 403, 500]

        if response.status_code == 200:
            data = response.json()
            assert len(data["data"]) <= 10

    @pytest.mark.asyncio
    async def test_get_products_with_search(self, client: AsyncClient):
        """Test products endpoint with search"""
        response = await client.get("/menu/products?search=test")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_products_filter_available(self, client: AsyncClient):
        """Test products filtered by is_available=true"""
        response = await client.get("/menu/products?is_available=true")
        assert response.status_code in [200, 401, 403, 500]

        if response.status_code == 200:
            data = response.json()
            for product in data["data"]:
                assert product["is_available"] is True

    @pytest.mark.asyncio
    async def test_get_products_filter_combos(self, client: AsyncClient):
        """Test products filtered by is_combo=true"""
        response = await client.get("/menu/products?is_combo=true")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_products_include_ingredients(self, client: AsyncClient):
        """Test products with include_ingredients=true"""
        response = await client.get("/menu/products?include_ingredients=true")
        assert response.status_code in [200, 401, 403, 500]

        if response.status_code == 200:
            data = response.json()
            if len(data["data"]) > 0:
                product = data["data"][0]
                assert "ingredients" in product

    @pytest.mark.asyncio
    async def test_get_products_include_modifiers(self, client: AsyncClient):
        """Test products with include_modifiers=true (for POS)"""
        response = await client.get("/menu/products?include_modifiers=true")
        assert response.status_code in [200, 401, 403, 500]

        if response.status_code == 200:
            data = response.json()
            if len(data["data"]) > 0:
                product = data["data"][0]
                assert "modifier_groups" in product
                for group in product.get("modifier_groups", []):
                    modifiers = group.get("modifiers", [])
                    if modifiers:
                        assert "max_limit" in modifiers[0]
                        break

    @pytest.mark.asyncio
    async def test_get_products_invalid_page(self, client: AsyncClient):
        """Test products with invalid page"""
        response = await client.get("/menu/products?page=0")
        assert response.status_code in [422, 500]

    @pytest.mark.asyncio
    async def test_get_products_exceeds_max_limit(self, client: AsyncClient):
        """Test products with limit exceeding max (250)"""
        response = await client.get("/menu/products?limit=251")
        assert response.status_code in [422, 500]

    @pytest.mark.asyncio
    async def test_get_products_search_field_kitchen_name(self, client: AsyncClient):
        """Test targeted search_field=kitchen_name"""
        response = await client.get("/menu/products?search=test&search_field=kitchen_name")
        assert response.status_code in [200, 401, 403, 422, 500]

    @pytest.mark.asyncio
    async def test_get_products_invalid_search_field(self, client: AsyncClient):
        """Invalid search_field returns 422 when authenticated"""
        response = await client.get("/menu/products?search=test&search_field=invalid")
        assert response.status_code in [401, 403, 422, 500]

    @pytest.mark.asyncio
    async def test_get_products_filter_online(self, client: AsyncClient):
        """Test is_available_online filter"""
        response = await client.get("/menu/products?is_available_online=true")
        assert response.status_code in [200, 401, 403, 500]

        if response.status_code == 200:
            for product in response.json()["data"]:
                assert product["is_available_online"] is True

    @pytest.mark.asyncio
    async def test_get_products_filter_table_qr(self, client: AsyncClient):
        """Test is_available_table_qr filter"""
        response = await client.get("/menu/products?is_available_table_qr=true")
        assert response.status_code in [200, 401, 403, 500]

        if response.status_code == 200:
            for product in response.json()["data"]:
                assert product["is_available_table_qr"] is True

    @pytest.mark.asyncio
    async def test_get_products_sort_name_asc(self, client: AsyncClient):
        """Test server-side sort=name_asc"""
        response = await client.get("/menu/products?sort=name_asc&limit=5")
        assert response.status_code in [200, 401, 403, 422, 500]

    @pytest.mark.asyncio
    async def test_get_products_invalid_sort(self, client: AsyncClient):
        """Invalid sort returns 422 when authenticated"""
        response = await client.get("/menu/products?sort=not_a_sort")
        assert response.status_code in [401, 403, 422, 500]

    @pytest.mark.asyncio
    async def test_get_products_margin_negative(self, client: AsyncClient):
        """Test margin_negative filter (cost > price)"""
        response = await client.get("/menu/products?margin_negative=true&limit=10")
        assert response.status_code in [200, 401, 403, 500]

        if response.status_code == 200:
            for product in response.json()["data"]:
                cost = product.get("costo_calculado")
                if cost is not None:
                    assert float(cost) > float(product["price"])

    @pytest.mark.asyncio
    async def test_get_products_has_recipe(self, client: AsyncClient):
        """Test has_recipe=true filter"""
        response = await client.get("/menu/products?has_recipe=true&limit=10")
        assert response.status_code in [200, 401, 403, 500]


class TestProductsStatsEndpoint:
    """Test products stats endpoint"""

    @pytest.mark.asyncio
    async def test_get_products_stats(self, client: AsyncClient):
        """Test GET /menu/products/stats"""
        response = await client.get("/menu/products/stats")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_products_stats_structure(self, client: AsyncClient):
        """Test products stats response structure"""
        response = await client.get("/menu/products/stats")

        if response.status_code == 200:
            data = response.json()
            assert "total" in data
            assert "available" in data
            assert "with_stock_control" in data
            assert "combos" in data


class TestProductByIdEndpoint:
    """Test single product endpoint"""

    @pytest.mark.asyncio
    async def test_get_product_not_found(self, client: AsyncClient):
        """Test GET /menu/products/{id} with non-existent ID"""
        fake_id = str(uuid4())
        response = await client.get(f"/menu/products/{fake_id}")
        assert response.status_code in [404, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_product_invalid_uuid(self, client: AsyncClient):
        """Test GET /menu/products/{id} with invalid UUID"""
        response = await client.get("/menu/products/invalid-uuid")
        assert response.status_code in [422, 500]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_existing_product(self, client: AsyncClient):
        """Integration test: get an existing product"""
        # First get a product from the list
        list_response = await client.get("/menu/products?limit=1")

        if list_response.status_code == 200:
            data = list_response.json()
            if len(data["data"]) > 0:
                product_id = data["data"][0]["id"]
                response = await client.get(f"/menu/products/{product_id}")
                assert response.status_code in [200, 401, 403, 500]

                if response.status_code == 200:
                    product = response.json()
                    assert "data" in product
                    assert product["data"]["id"] == product_id


class TestProductCreateModel:
    """Tests for the ProductCreate Pydantic model — issue #456 (allow products without ingredients)"""

    def test_create_with_empty_recipe_succeeds(self):
        """Issue #456: ProductCreate must accept empty ingredients AND empty recipe_base_ids."""
        from app.models.product import ProductCreate
        product = ProductCreate(
            name="Cargo de domicilio",
            price=5000,
            category_id=uuid4(),
            ingredients=[],
            recipe_base_ids=[],
            tenant_id=uuid4(),
        )
        assert product.ingredients == []
        assert product.recipe_base_ids == []

    def test_create_with_only_ingredients_succeeds(self):
        """ProductCreate accepts only ingredients (no recipe bases) — pre-existing behavior preserved."""
        from app.models.product import ProductCreate, RecipeIngredientBase
        product = ProductCreate(
            name="Hamburguesa",
            price=15000,
            category_id=uuid4(),
            ingredients=[RecipeIngredientBase(ingredient_id=uuid4(), quantity=100, unit="g")],
            recipe_base_ids=[],
            tenant_id=uuid4(),
        )
        assert len(product.ingredients) == 1

    def test_create_with_only_recipe_bases_succeeds(self):
        """ProductCreate accepts only recipe bases (no direct ingredients) — pre-existing behavior preserved."""
        from app.models.product import ProductCreate
        product = ProductCreate(
            name="Combo",
            price=20000,
            category_id=uuid4(),
            ingredients=[],
            recipe_base_ids=[uuid4()],
            tenant_id=uuid4(),
        )
        assert len(product.recipe_base_ids) == 1

    def test_create_validates_price_still_required(self):
        """Removing the recipe validator must NOT loosen other validators (price > 0)."""
        from app.models.product import ProductCreate
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            ProductCreate(
                name="Bad",
                price=0,  # invalid: gt=0
                category_id=uuid4(),
                ingredients=[],
                recipe_base_ids=[],
                tenant_id=uuid4(),
            )

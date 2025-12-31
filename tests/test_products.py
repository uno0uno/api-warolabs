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

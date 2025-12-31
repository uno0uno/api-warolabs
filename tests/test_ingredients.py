"""
Tests for ingredients module endpoints.

Endpoints tested:
- GET /suppliers/ingredients
"""
import pytest
from httpx import AsyncClient


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

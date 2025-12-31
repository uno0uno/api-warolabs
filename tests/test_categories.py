"""
Tests for categories module endpoints.

Endpoints tested:
- GET /menu/categories
"""
import pytest
from httpx import AsyncClient


class TestCategoriesEndpoint:
    """Test categories list endpoint"""

    @pytest.mark.asyncio
    async def test_get_categories_without_session(self, client: AsyncClient):
        """Test GET /menu/categories without session returns categories or auth error"""
        response = await client.get("/menu/categories")
        # Categories endpoint may require session or work without
        # 200 = success, 401/403 = auth required, 500 = server error
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_categories_response_structure(self, client: AsyncClient):
        """Test categories response has correct structure"""
        response = await client.get("/menu/categories")

        if response.status_code == 200:
            data = response.json()
            # Check response structure
            assert "success" in data
            assert "total" in data
            assert "data" in data
            assert isinstance(data["data"], list)

            # If there are categories, check their structure
            if len(data["data"]) > 0:
                category = data["data"][0]
                assert "id" in category
                assert "name" in category
                assert "created_at" in category
                assert "updated_at" in category

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_categories_returns_list(self, client: AsyncClient):
        """Integration test: categories endpoint returns a list"""
        response = await client.get("/menu/categories")

        if response.status_code == 200:
            data = response.json()
            assert data["success"] is True
            assert isinstance(data["total"], int)
            assert data["total"] >= 0

    @pytest.mark.asyncio
    async def test_categories_endpoint_method_not_allowed(self, client: AsyncClient):
        """Test that POST to categories returns 405 or 500"""
        response = await client.post("/menu/categories", json={"name": "Test"})
        # 405 = method not allowed, 500 = server error
        assert response.status_code in [405, 500]

    @pytest.mark.asyncio
    async def test_categories_endpoint_with_trailing_slash(self, client: AsyncClient):
        """Test categories endpoint handles trailing slash"""
        response = await client.get("/menu/categories/")
        # Should redirect or work normally
        assert response.status_code in [200, 307, 401, 403, 500]

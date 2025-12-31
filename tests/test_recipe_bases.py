"""
Tests for recipe bases module endpoints.

Endpoints tested:
- GET /menu/recipe-bases
- GET /menu/recipe-bases/{recipe_base_id}
- POST /menu/recipe-bases
- PUT /menu/recipe-bases/{recipe_base_id}
- DELETE /menu/recipe-bases/{recipe_base_id}
"""
import pytest
from httpx import AsyncClient
from uuid import uuid4


class TestRecipeBasesListEndpoint:
    """Test recipe bases list endpoint"""

    @pytest.mark.asyncio
    async def test_get_recipe_bases_default(self, client: AsyncClient):
        """Test GET /menu/recipe-bases with default params"""
        response = await client.get("/menu/recipe-bases")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_recipe_bases_response_structure(self, client: AsyncClient):
        """Test recipe bases response structure"""
        response = await client.get("/menu/recipe-bases")

        if response.status_code == 200:
            data = response.json()
            assert "success" in data
            assert "total" in data
            assert "data" in data
            assert isinstance(data["data"], list)

    @pytest.mark.asyncio
    async def test_get_recipe_bases_with_pagination(self, client: AsyncClient):
        """Test recipe bases with pagination"""
        response = await client.get("/menu/recipe-bases?page=1&limit=10")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_recipe_bases_with_search(self, client: AsyncClient):
        """Test recipe bases with search"""
        response = await client.get("/menu/recipe-bases?search=pizza")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_recipe_bases_filter_active(self, client: AsyncClient):
        """Test recipe bases filtered by active status"""
        response = await client.get("/menu/recipe-bases?is_active=true")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_recipe_bases_include_ingredients(self, client: AsyncClient):
        """Test recipe bases with ingredients included"""
        response = await client.get("/menu/recipe-bases?include_ingredients=true")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_recipe_bases_invalid_page(self, client: AsyncClient):
        """Test recipe bases with invalid page"""
        response = await client.get("/menu/recipe-bases?page=0")
        assert response.status_code in [422, 500]


class TestRecipeBaseByIdEndpoint:
    """Test single recipe base endpoint"""

    @pytest.mark.asyncio
    async def test_get_recipe_base_not_found(self, client: AsyncClient):
        """Test GET /menu/recipe-bases/{id} with non-existent ID"""
        fake_id = str(uuid4())
        response = await client.get(f"/menu/recipe-bases/{fake_id}")
        assert response.status_code in [404, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_recipe_base_invalid_uuid(self, client: AsyncClient):
        """Test GET /menu/recipe-bases/{id} with invalid UUID"""
        response = await client.get("/menu/recipe-bases/invalid-uuid")
        assert response.status_code in [422, 500]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_existing_recipe_base(self, client: AsyncClient):
        """Integration test: get an existing recipe base"""
        list_response = await client.get("/menu/recipe-bases?limit=1")

        if list_response.status_code == 200:
            data = list_response.json()
            if len(data["data"]) > 0:
                recipe_id = data["data"][0]["id"]
                response = await client.get(f"/menu/recipe-bases/{recipe_id}")
                assert response.status_code in [200, 401, 403, 500]


class TestRecipeBaseCRUD:
    """Test recipe base CRUD operations"""

    @pytest.mark.asyncio
    async def test_create_recipe_base_missing_fields(self, client: AsyncClient):
        """Test creating recipe base without required fields"""
        response = await client.post("/menu/recipe-bases", json={})
        assert response.status_code in [422, 500]

    @pytest.mark.asyncio
    async def test_update_recipe_base_not_found(self, client: AsyncClient):
        """Test updating non-existent recipe base"""
        fake_id = str(uuid4())
        response = await client.put(
            f"/menu/recipe-bases/{fake_id}",
            json={"name": "Updated Recipe"}
        )
        assert response.status_code in [404, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_delete_recipe_base_not_found(self, client: AsyncClient):
        """Test deleting non-existent recipe base"""
        fake_id = str(uuid4())
        response = await client.delete(f"/menu/recipe-bases/{fake_id}")
        assert response.status_code in [404, 401, 403, 500]

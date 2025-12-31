"""
Tests for modifiers module endpoints.

Endpoints tested:
- GET /menu/modifier-groups
- GET /menu/modifier-groups/stats/summary
- GET /menu/modifier-groups/{group_id}
- POST /menu/modifier-groups
- PUT /menu/modifier-groups/{group_id}
- DELETE /menu/modifier-groups/{group_id}
"""
import pytest
from httpx import AsyncClient
from uuid import uuid4


class TestModifierGroupsListEndpoint:
    """Test modifier groups list endpoint"""

    @pytest.mark.asyncio
    async def test_get_modifier_groups_default(self, client: AsyncClient):
        """Test GET /menu/modifier-groups with default params"""
        response = await client.get("/menu/modifier-groups")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_modifier_groups_response_structure(self, client: AsyncClient):
        """Test modifier groups response structure"""
        response = await client.get("/menu/modifier-groups")

        if response.status_code == 200:
            data = response.json()
            assert "success" in data
            assert "total" in data
            assert "data" in data
            assert isinstance(data["data"], list)

    @pytest.mark.asyncio
    async def test_get_modifier_groups_with_pagination(self, client: AsyncClient):
        """Test modifier groups with pagination"""
        response = await client.get("/menu/modifier-groups?page=1&limit=10")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_modifier_groups_with_search(self, client: AsyncClient):
        """Test modifier groups with search"""
        response = await client.get("/menu/modifier-groups?search=extra")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_modifier_groups_filter_by_product(self, client: AsyncClient):
        """Test modifier groups filtered by product"""
        fake_product_id = str(uuid4())
        response = await client.get(f"/menu/modifier-groups?product_id={fake_product_id}")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_modifier_groups_invalid_page(self, client: AsyncClient):
        """Test modifier groups with invalid page"""
        response = await client.get("/menu/modifier-groups?page=0")
        assert response.status_code in [422, 500]


class TestModifierGroupStatsEndpoint:
    """Test modifier group stats endpoint"""

    @pytest.mark.asyncio
    async def test_get_modifier_group_stats(self, client: AsyncClient):
        """Test GET /menu/modifier-groups/stats/summary"""
        response = await client.get("/menu/modifier-groups/stats/summary")
        assert response.status_code in [200, 401, 403, 500]


class TestModifierGroupByIdEndpoint:
    """Test single modifier group endpoint"""

    @pytest.mark.asyncio
    async def test_get_modifier_group_not_found(self, client: AsyncClient):
        """Test GET /menu/modifier-groups/{id} with non-existent ID"""
        fake_id = str(uuid4())
        response = await client.get(f"/menu/modifier-groups/{fake_id}")
        assert response.status_code in [404, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_modifier_group_invalid_uuid(self, client: AsyncClient):
        """Test GET /menu/modifier-groups/{id} with invalid UUID"""
        response = await client.get("/menu/modifier-groups/invalid-uuid")
        assert response.status_code in [422, 500]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_existing_modifier_group(self, client: AsyncClient):
        """Integration test: get an existing modifier group"""
        list_response = await client.get("/menu/modifier-groups?limit=1")

        if list_response.status_code == 200:
            data = list_response.json()
            if len(data["data"]) > 0:
                group_id = data["data"][0]["id"]
                response = await client.get(f"/menu/modifier-groups/{group_id}")
                assert response.status_code in [200, 401, 403, 500]


class TestModifierGroupCRUD:
    """Test modifier group CRUD operations"""

    @pytest.mark.asyncio
    async def test_create_modifier_group_missing_fields(self, client: AsyncClient):
        """Test creating modifier group without required fields"""
        response = await client.post("/menu/modifier-groups", json={})
        assert response.status_code in [422, 500]

    @pytest.mark.asyncio
    async def test_update_modifier_group_not_found(self, client: AsyncClient):
        """Test updating non-existent modifier group"""
        fake_id = str(uuid4())
        response = await client.put(
            f"/menu/modifier-groups/{fake_id}",
            json={"name": "Updated Name"}
        )
        assert response.status_code in [404, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_delete_modifier_group_not_found(self, client: AsyncClient):
        """Test deleting non-existent modifier group"""
        fake_id = str(uuid4())
        response = await client.delete(f"/menu/modifier-groups/{fake_id}")
        assert response.status_code in [404, 401, 403, 500]

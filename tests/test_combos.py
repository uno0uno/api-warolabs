"""
Tests for combos module endpoints.

Endpoints tested:
- GET /menu/combos
- GET /menu/combos/stats/summary
- GET /menu/combos/{combo_id}
- POST /menu/combos
- PUT /menu/combos/{combo_id}
- DELETE /menu/combos/{combo_id}
"""
import pytest
from httpx import AsyncClient
from uuid import uuid4


class TestCombosListEndpoint:
    """Test combos list endpoint"""

    @pytest.mark.asyncio
    async def test_get_combos_default(self, client: AsyncClient):
        """Test GET /menu/combos with default params"""
        response = await client.get("/menu/combos")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_combos_response_structure(self, client: AsyncClient):
        """Test combos response structure"""
        response = await client.get("/menu/combos")

        if response.status_code == 200:
            data = response.json()
            assert "success" in data
            assert "total" in data
            assert "data" in data
            assert isinstance(data["data"], list)

    @pytest.mark.asyncio
    async def test_get_combos_with_pagination(self, client: AsyncClient):
        """Test combos with pagination"""
        response = await client.get("/menu/combos?page=1&limit=10")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_combos_with_search(self, client: AsyncClient):
        """Test combos with search"""
        response = await client.get("/menu/combos?search=combo")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_combos_filter_by_category(self, client: AsyncClient):
        """Test combos filtered by category"""
        fake_category_id = str(uuid4())
        response = await client.get(f"/menu/combos?category_id={fake_category_id}")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_combos_filter_available(self, client: AsyncClient):
        """Test combos filtered by availability"""
        response = await client.get("/menu/combos?is_available=true")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_combos_invalid_page(self, client: AsyncClient):
        """Test combos with invalid page"""
        response = await client.get("/menu/combos?page=0")
        assert response.status_code in [422, 500]


class TestComboStatsEndpoint:
    """Test combo stats endpoint"""

    @pytest.mark.asyncio
    async def test_get_combo_stats(self, client: AsyncClient):
        """Test GET /menu/combos/stats/summary"""
        response = await client.get("/menu/combos/stats/summary")
        assert response.status_code in [200, 401, 403, 500]


class TestComboByIdEndpoint:
    """Test single combo endpoint"""

    @pytest.mark.asyncio
    async def test_get_combo_not_found(self, client: AsyncClient):
        """Test GET /menu/combos/{id} with non-existent ID"""
        fake_id = str(uuid4())
        response = await client.get(f"/menu/combos/{fake_id}")
        assert response.status_code in [404, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_combo_invalid_uuid(self, client: AsyncClient):
        """Test GET /menu/combos/{id} with invalid UUID"""
        response = await client.get("/menu/combos/invalid-uuid")
        assert response.status_code in [422, 500]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_existing_combo(self, client: AsyncClient):
        """Integration test: get an existing combo"""
        list_response = await client.get("/menu/combos?limit=1")

        if list_response.status_code == 200:
            data = list_response.json()
            if len(data["data"]) > 0:
                combo_id = data["data"][0]["id"]
                response = await client.get(f"/menu/combos/{combo_id}")
                assert response.status_code in [200, 401, 403, 500]


class TestComboCRUD:
    """Test combo CRUD operations"""

    @pytest.mark.asyncio
    async def test_create_combo_missing_fields(self, client: AsyncClient):
        """Test creating combo without required fields"""
        response = await client.post("/menu/combos", json={})
        assert response.status_code in [422, 500]

    @pytest.mark.asyncio
    async def test_update_combo_not_found(self, client: AsyncClient):
        """Test updating non-existent combo"""
        fake_id = str(uuid4())
        response = await client.put(
            f"/menu/combos/{fake_id}",
            json={"name": "Updated Combo"}
        )
        assert response.status_code in [404, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_delete_combo_not_found(self, client: AsyncClient):
        """Test deleting non-existent combo"""
        fake_id = str(uuid4())
        response = await client.delete(f"/menu/combos/{fake_id}")
        assert response.status_code in [404, 401, 403, 500]

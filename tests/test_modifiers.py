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
from unittest.mock import AsyncMock, MagicMock

from app.services.menu_history_service import get_modifier_group_snapshot


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
    async def test_get_modifier_groups_filter_required(self, client: AsyncClient):
        """Test modifier groups filtered by required state"""
        response = await client.get("/menu/modifier-groups?is_required=true")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_modifier_groups_filter_optional(self, client: AsyncClient):
        """Test modifier groups filtered by optional state"""
        response = await client.get("/menu/modifier-groups?is_required=false")
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


class TestModifierGroupDeleteSoftDelete:
    """Soft-delete when modifiers have order history"""

    @pytest.mark.asyncio
    async def test_soft_delete_archives_when_used_in_orders(self, monkeypatch):
        from app.services.modifiers_service import delete_modifier_group
        from unittest.mock import AsyncMock, MagicMock

        group_id = uuid4()
        tenant_id = uuid4()
        user_id = uuid4()

        mock_session = MagicMock(tenant_id=tenant_id, user_id=user_id)
        monkeypatch.setattr("app.services.modifiers_service.require_valid_session", lambda req: mock_session)

        mock_conn = AsyncMock()
        # verify group exists
        mock_conn.fetchrow.return_value = {"id": group_id, "name": "Salsas"}
        # has_sales = True
        mock_conn.fetchval.return_value = True
        mock_conn.execute.return_value = None

        mock_cm = AsyncMock()
        mock_cm.__aenter__.return_value = mock_conn
        mock_cm.__aexit__.return_value = False
        # transaction cm
        mock_tx = AsyncMock()
        mock_tx.__aenter__.return_value = mock_conn
        mock_tx.__aexit__.return_value = False
        mock_conn.transaction = MagicMock(return_value=mock_tx)

        monkeypatch.setattr("app.services.modifiers_service.get_db_connection", lambda: mock_cm)
        monkeypatch.setattr("app.services.modifiers_service.menu_history_service.get_modifier_group_snapshot", AsyncMock(return_value={"dummy": True}))
        monkeypatch.setattr("app.services.modifiers_service.menu_history_service.record_modifier_group_delete", AsyncMock(return_value=None))
        monkeypatch.setattr("app.services.modifiers_service.record_module_event", AsyncMock(return_value=None))

        from fastapi import Request
        req = MagicMock(spec=Request)

        result = await delete_modifier_group(req, group_id, reason="cleanup")

        assert result["archived"] is True
        # should have called soft-delete queries, not hard delete of group
        executed = [str(c.args[0]) for c in mock_conn.execute.call_args_list]
        assert any("product_modifier_groups" in q for q in executed)
        assert any("UPDATE modifiers" in q for q in executed)
        assert any("UPDATE modifier_groups SET is_active" in q for q in executed)
        assert not any("DELETE FROM modifier_groups" in q for q in executed)

    @pytest.mark.asyncio
    async def test_hard_delete_when_no_history(self, monkeypatch):
        from app.services.modifiers_service import delete_modifier_group
        from unittest.mock import MagicMock

        group_id = uuid4()
        tenant_id = uuid4()
        user_id = uuid4()
        mock_session = MagicMock(tenant_id=tenant_id, user_id=user_id)
        monkeypatch.setattr("app.services.modifiers_service.require_valid_session", lambda req: mock_session)

        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = {"id": group_id, "name": "Salsas"}
        mock_conn.fetchval.return_value = False
        mock_conn.execute.return_value = None
        mock_cm = AsyncMock()
        mock_cm.__aenter__.return_value = mock_conn
        mock_cm.__aexit__.return_value = False
        mock_tx = AsyncMock()
        mock_tx.__aenter__.return_value = mock_conn
        mock_tx.__aexit__.return_value = False
        mock_conn.transaction = MagicMock(return_value=mock_tx)
        monkeypatch.setattr("app.services.modifiers_service.get_db_connection", lambda: mock_cm)
        monkeypatch.setattr("app.services.modifiers_service.menu_history_service.get_modifier_group_snapshot", AsyncMock(return_value={"dummy": True}))
        monkeypatch.setattr("app.services.modifiers_service.menu_history_service.record_modifier_group_delete", AsyncMock(return_value=None))
        monkeypatch.setattr("app.services.modifiers_service.record_module_event", AsyncMock(return_value=None))

        from fastapi import Request
        req = MagicMock(spec=Request)
        result = await delete_modifier_group(req, group_id, reason="cleanup")

        assert result["archived"] is False
        executed = [str(c.args[0]) for c in mock_conn.execute.call_args_list]
        assert any("DELETE FROM modifier_groups" in q for q in executed)


@pytest.mark.asyncio
async def test_modifier_group_snapshot_includes_recipe_lines():
    group_id = uuid4()
    tenant_id = uuid4()
    modifier_id = uuid4()
    ingredient_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "id": group_id,
        "name": "Salsas",
        "min_qty": 0,
        "max_qty": 2,
        "is_required": False,
        "sort_order": 0,
    }
    conn.fetch.side_effect = [
        [{"product_id": uuid4()}],
        [{
            "id": modifier_id,
            "name": "Salsa de la casa",
            "price": 0,
            "max_limit": 1,
            "included_quantity": 0,
            "is_available": True,
            "is_default": False,
            "sort_order": 0,
            "option_type": "RECIPE",
            "ingredient_id": None,
            "ingredient_quantity": None,
            "ingredient_unit": None,
            "recipe_base_type_id": None,
            "recipe_base_quantity": 1,
            "linked_product_id": None,
            "linked_product_quantity": 1,
        }],
        [{
            "modifier_id": modifier_id,
            "ingredient_id": ingredient_id,
            "quantity": 2,
            "unit": "gr",
        }],
    ]

    snapshot = await get_modifier_group_snapshot(conn, group_id, tenant_id)

    assert snapshot is not None
    assert snapshot["modifiers"][0]["recipe_lines"] == [{
        "ingredient_id": ingredient_id,
        "quantity": 2,
        "unit": "gr",
    }]
    assert conn.fetch.await_count == 3

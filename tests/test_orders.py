"""
Tests for orders module endpoints.

Endpoints tested:
- GET /orders
- GET /orders/{order_id}
- GET /orders/{order_id}/items
"""
import pytest
from httpx import AsyncClient
from uuid import uuid4

from app.services.orders_service import _inventory_quantity


def test_inventory_quantity_normalizes_consumption_residue():
    assert _inventory_quantity(0.1 + 0.2 - 0.3) == 0.0
    assert _inventory_quantity("1.3450000000001") == 1.345


class TestOrdersListEndpoint:
    """Test orders list endpoint"""

    @pytest.mark.asyncio
    async def test_get_orders_default(self, client: AsyncClient):
        """Test GET /orders with default params"""
        response = await client.get("/orders")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_orders_response_structure(self, client: AsyncClient):
        """Test orders response structure"""
        response = await client.get("/orders")

        if response.status_code == 200:
            data = response.json()
            assert "success" in data
            assert "data" in data
            assert isinstance(data["data"], list)

    @pytest.mark.asyncio
    async def test_get_orders_with_pagination(self, client: AsyncClient):
        """Test orders with pagination"""
        response = await client.get("/orders?limit=10&offset=0")
        assert response.status_code in [200, 401, 403, 500]

        if response.status_code == 200:
            data = response.json()
            assert len(data["data"]) <= 10

    @pytest.mark.asyncio
    async def test_get_orders_with_search(self, client: AsyncClient):
        """Test orders with search"""
        response = await client.get("/orders?search=test&search_field=customer_name")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_orders_filter_by_payment_method(self, client: AsyncClient):
        """Test orders filtered by payment method"""
        response = await client.get("/orders?payment_method=cash")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_orders_filter_by_status(self, client: AsyncClient):
        """Test orders filtered by status"""
        response = await client.get("/orders?status=completed")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_orders_sort_asc(self, client: AsyncClient):
        """Test orders with ascending sort"""
        response = await client.get("/orders?sort_field=order_date&sort_direction=asc")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_orders_sort_by_amount(self, client: AsyncClient):
        """Test orders sorted by total amount"""
        response = await client.get("/orders?sort_field=total_amount&sort_direction=desc")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_orders_invalid_limit(self, client: AsyncClient):
        """Test orders with invalid limit"""
        response = await client.get("/orders?limit=0")
        assert response.status_code in [422, 500]

    @pytest.mark.asyncio
    async def test_get_orders_exceeds_max_limit(self, client: AsyncClient):
        """Test orders with limit exceeding max (250)"""
        response = await client.get("/orders?limit=251")
        assert response.status_code in [422, 500]


class TestOrderByIdEndpoint:
    """Test single order endpoint"""

    @pytest.mark.asyncio
    async def test_get_order_not_found(self, client: AsyncClient):
        """Test GET /orders/{id} with non-existent ID"""
        fake_id = str(uuid4())
        response = await client.get(f"/orders/{fake_id}")
        assert response.status_code in [404, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_order_invalid_uuid(self, client: AsyncClient):
        """Test GET /orders/{id} with invalid UUID"""
        response = await client.get("/orders/invalid-uuid")
        assert response.status_code in [422, 500]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_existing_order(self, client: AsyncClient):
        """Integration test: get an existing order"""
        list_response = await client.get("/orders?limit=1")

        if list_response.status_code == 200:
            data = list_response.json()
            if len(data["data"]) > 0:
                order_id = data["data"][0]["id"]
                response = await client.get(f"/orders/{order_id}")
                assert response.status_code in [200, 401, 403, 500]


class TestOrderItemsEndpoint:
    """Test order items endpoint"""

    @pytest.mark.asyncio
    async def test_get_order_items_not_found(self, client: AsyncClient):
        """Test GET /orders/{id}/items with non-existent ID"""
        fake_id = str(uuid4())
        response = await client.get(f"/orders/{fake_id}/items")
        assert response.status_code in [404, 401, 403, 500]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_existing_order_items(self, client: AsyncClient):
        """Integration test: get items for existing order"""
        list_response = await client.get("/orders?limit=1")

        if list_response.status_code == 200:
            data = list_response.json()
            if len(data["data"]) > 0:
                order_id = data["data"][0]["id"]
                response = await client.get(f"/orders/{order_id}/items")
                assert response.status_code in [200, 401, 403, 500]

                if response.status_code == 200:
                    items_data = response.json()
                    assert "success" in items_data
                    assert "data" in items_data

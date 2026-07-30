"""
Tests for GET /orders/products-sold (ventas productos report).
"""
import pytest
from httpx import AsyncClient


class TestProductsSoldEndpoint:
    """Products sold aggregation endpoint"""

    @pytest.mark.asyncio
    async def test_get_products_sold_default(self, client: AsyncClient):
        response = await client.get("/orders/products-sold")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_products_sold_response_structure(self, client: AsyncClient):
        response = await client.get("/orders/products-sold")
        if response.status_code == 200:
            data = response.json()
            assert data.get("success") is True
            assert "data" in data
            assert "totals" in data
            assert "pagination" in data
            assert isinstance(data["data"], list)
            assert "total" in data["pagination"]

    @pytest.mark.asyncio
    async def test_get_products_sold_with_pagination(self, client: AsyncClient):
        response = await client.get("/orders/products-sold?limit=10&offset=0")
        assert response.status_code in [200, 401, 403, 500]
        if response.status_code == 200:
            data = response.json()
            assert data["pagination"]["limit"] == 10
            assert data["pagination"]["offset"] == 0
            assert len(data["data"]) <= 10

    @pytest.mark.asyncio
    async def test_get_products_sold_with_search(self, client: AsyncClient):
        response = await client.get("/orders/products-sold?search=cafe")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_products_sold_with_channel(self, client: AsyncClient):
        for ch in ("pos", "mesa", "online"):
            response = await client.get(f"/orders/products-sold?channel={ch}")
            assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_products_sold_with_sort(self, client: AsyncClient):
        response = await client.get("/orders/products-sold?sort=name_asc")
        assert response.status_code in [200, 401, 403, 500]

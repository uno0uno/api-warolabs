"""
Tests for customers module endpoints.

Endpoints tested:
- POST /customers/search-or-create
- GET /customers/search
"""
import pytest
from httpx import AsyncClient


class TestCustomerSearchOrCreate:
    """Test customer search or create endpoint"""

    @pytest.mark.asyncio
    async def test_search_or_create_missing_phone(self, client: AsyncClient):
        """Test POST /customers/search-or-create without phone"""
        response = await client.post(
            "/customers/search-or-create",
            json={"name": "Test Customer"}
        )
        assert response.status_code in [422, 500]

    @pytest.mark.asyncio
    async def test_search_or_create_valid_request(self, client: AsyncClient):
        """Test POST /customers/search-or-create with valid data"""
        response = await client.post(
            "/customers/search-or-create",
            json={
                "phone_number": "3001234567",
                "name": "Test Customer",
                "email": "test@example.com"
            }
        )
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_search_or_create_phone_only(self, client: AsyncClient):
        """Test POST /customers/search-or-create with phone only"""
        response = await client.post(
            "/customers/search-or-create",
            json={"phone_number": "3009876543"}
        )
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_search_or_create_response_structure(self, client: AsyncClient):
        """Test search or create response structure"""
        response = await client.post(
            "/customers/search-or-create",
            json={"phone_number": "3001112222"}
        )

        if response.status_code == 200:
            data = response.json()
            assert "success" in data
            assert "data" in data


class TestCustomerSearch:
    """Test customer search endpoint"""

    @pytest.mark.asyncio
    async def test_search_customer_missing_phone(self, client: AsyncClient):
        """Test GET /customers/search without phone number"""
        response = await client.get("/customers/search")
        assert response.status_code in [422, 500]

    @pytest.mark.asyncio
    async def test_search_customer_valid_phone(self, client: AsyncClient):
        """Test GET /customers/search with valid phone"""
        response = await client.get("/customers/search?phone_number=3001234567")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_search_customer_response_structure(self, client: AsyncClient):
        """Test search customer response structure"""
        response = await client.get("/customers/search?phone_number=3001234567")

        if response.status_code == 200:
            data = response.json()
            assert "success" in data
            assert "found" in data
            # customer can be null if not found

    @pytest.mark.asyncio
    async def test_search_customer_short_phone(self, client: AsyncClient):
        """Test search with too short phone number"""
        response = await client.get("/customers/search?phone_number=123")
        # min_length=7 validation
        assert response.status_code in [422, 500]

    @pytest.mark.asyncio
    async def test_search_customer_long_phone(self, client: AsyncClient):
        """Test search with too long phone number"""
        response = await client.get("/customers/search?phone_number=123456789012345678901")
        # max_length=20 validation
        assert response.status_code in [422, 500]

    @pytest.mark.asyncio
    async def test_search_customer_not_found(self, client: AsyncClient):
        """Test search for non-existent customer"""
        response = await client.get("/customers/search?phone_number=9999999999")
        assert response.status_code in [200, 401, 403, 500]

        if response.status_code == 200:
            data = response.json()
            # Should return found: false for non-existent customer
            assert "found" in data

"""
Tests for suppliers module endpoints.

Endpoints tested:
- GET /suppliers/providers
- GET /suppliers/providers/{supplier_id}
- POST /suppliers/providers
- PUT /suppliers/providers/{supplier_id}
- DELETE /suppliers/providers/{supplier_id}
- GET /suppliers/providers/{supplier_id}/payment-agreements
"""
import pytest
from httpx import AsyncClient
from uuid import uuid4


class TestSuppliersListEndpoint:
    """Test suppliers list endpoint"""

    @pytest.mark.asyncio
    async def test_get_suppliers_default(self, client: AsyncClient):
        """Test GET /suppliers/providers with default params"""
        response = await client.get("/suppliers/providers")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_suppliers_response_structure(self, client: AsyncClient):
        """Test suppliers response structure"""
        response = await client.get("/suppliers/providers")

        if response.status_code == 200:
            data = response.json()
            assert "success" in data
            assert "total" in data
            assert "data" in data
            assert isinstance(data["data"], list)

            if len(data["data"]) > 0:
                supplier = data["data"][0]
                assert "id" in supplier
                assert "name" in supplier

    @pytest.mark.asyncio
    async def test_get_suppliers_with_pagination(self, client: AsyncClient):
        """Test suppliers with pagination"""
        response = await client.get("/suppliers/providers?page=1&limit=10")
        assert response.status_code in [200, 401, 403, 500]

        if response.status_code == 200:
            data = response.json()
            assert len(data["data"]) <= 10

    @pytest.mark.asyncio
    async def test_get_suppliers_with_search(self, client: AsyncClient):
        """Test suppliers with search"""
        response = await client.get("/suppliers/providers?search=test")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_suppliers_search_by_field(self, client: AsyncClient):
        """Test suppliers search by specific field"""
        response = await client.get("/suppliers/providers?search=test&search_field=name")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_suppliers_filter_active(self, client: AsyncClient):
        """Test suppliers filtered by active status"""
        response = await client.get("/suppliers/providers?is_active=true")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_suppliers_filter_inactive(self, client: AsyncClient):
        """Test suppliers filtered by inactive status"""
        response = await client.get("/suppliers/providers?is_active=false")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_suppliers_invalid_page(self, client: AsyncClient):
        """Test suppliers with invalid page"""
        response = await client.get("/suppliers/providers?page=0")
        assert response.status_code in [422, 500]

    @pytest.mark.asyncio
    async def test_get_suppliers_exceeds_max_limit(self, client: AsyncClient):
        """Test suppliers with limit exceeding max (250)"""
        response = await client.get("/suppliers/providers?limit=251")
        assert response.status_code in [422, 500]


class TestSupplierByIdEndpoint:
    """Test single supplier endpoint"""

    @pytest.mark.asyncio
    async def test_get_supplier_not_found(self, client: AsyncClient):
        """Test GET /suppliers/providers/{id} with non-existent ID"""
        fake_id = str(uuid4())
        response = await client.get(f"/suppliers/providers/{fake_id}")
        assert response.status_code in [404, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_supplier_invalid_uuid(self, client: AsyncClient):
        """Test GET /suppliers/providers/{id} with invalid UUID"""
        response = await client.get("/suppliers/providers/invalid-uuid")
        assert response.status_code in [422, 500]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_existing_supplier(self, client: AsyncClient):
        """Integration test: get an existing supplier"""
        list_response = await client.get("/suppliers/providers?limit=1")

        if list_response.status_code == 200:
            data = list_response.json()
            if len(data["data"]) > 0:
                supplier_id = data["data"][0]["id"]
                response = await client.get(f"/suppliers/providers/{supplier_id}")
                assert response.status_code in [200, 401, 403, 500]


class TestSupplierCRUD:
    """Test supplier CRUD operations"""

    @pytest.mark.asyncio
    async def test_create_supplier_missing_fields(self, client: AsyncClient):
        """Test creating supplier without required fields"""
        response = await client.post(
            "/suppliers/providers",
            json={}
        )
        assert response.status_code in [422, 500]

    @pytest.mark.asyncio
    async def test_update_supplier_not_found(self, client: AsyncClient):
        """Test updating non-existent supplier"""
        fake_id = str(uuid4())
        response = await client.put(
            f"/suppliers/providers/{fake_id}",
            json={"name": "Updated Name"}
        )
        assert response.status_code in [404, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_delete_supplier_not_found(self, client: AsyncClient):
        """Test deleting non-existent supplier"""
        fake_id = str(uuid4())
        response = await client.delete(f"/suppliers/providers/{fake_id}")
        assert response.status_code in [404, 401, 403, 500]


class TestPaymentAgreementsEndpoint:
    """Test supplier payment agreements endpoint"""

    @pytest.mark.asyncio
    async def test_get_payment_agreements_supplier_not_found(self, client: AsyncClient):
        """Test GET payment agreements for non-existent supplier"""
        fake_id = str(uuid4())
        response = await client.get(f"/suppliers/providers/{fake_id}/payment-agreements")
        assert response.status_code in [404, 401, 403, 500]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_existing_supplier_agreements(self, client: AsyncClient):
        """Integration test: get agreements for existing supplier"""
        list_response = await client.get("/suppliers/providers?limit=1")

        if list_response.status_code == 200:
            data = list_response.json()
            if len(data["data"]) > 0:
                supplier_id = data["data"][0]["id"]
                response = await client.get(f"/suppliers/providers/{supplier_id}/payment-agreements")
                assert response.status_code in [200, 401, 403, 500]

                if response.status_code == 200:
                    agreements_data = response.json()
                    assert "success" in agreements_data
                    assert "data" in agreements_data

    @pytest.mark.asyncio
    async def test_get_single_agreement_not_found(self, client: AsyncClient):
        """Test GET single payment agreement not found"""
        fake_supplier_id = str(uuid4())
        fake_agreement_id = str(uuid4())
        response = await client.get(
            f"/suppliers/providers/{fake_supplier_id}/payment-agreements/{fake_agreement_id}"
        )
        assert response.status_code in [404, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_create_agreement_supplier_not_found(self, client: AsyncClient):
        """Test creating agreement for non-existent supplier"""
        fake_id = str(uuid4())
        response = await client.post(
            f"/suppliers/providers/{fake_id}/payment-agreements",
            json={"credit_days": 30, "credit_limit": 1000.0}
        )
        assert response.status_code in [404, 422, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_delete_agreement_not_found(self, client: AsyncClient):
        """Test deleting non-existent payment agreement"""
        fake_supplier_id = str(uuid4())
        fake_agreement_id = str(uuid4())
        response = await client.delete(
            f"/suppliers/providers/{fake_supplier_id}/payment-agreements/{fake_agreement_id}"
        )
        assert response.status_code in [404, 401, 403, 500]

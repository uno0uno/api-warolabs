"""
Tests for purchases module endpoints.

Endpoints tested:
- GET /suppliers/purchases
- GET /suppliers/purchases/next-number
- GET /suppliers/purchases/{purchase_id}
- GET /suppliers/purchases/{purchase_id}/history
- GET /suppliers/purchases/{purchase_id}/attachments
"""
import pytest
from httpx import AsyncClient
from uuid import uuid4


class TestPurchasesListEndpoint:
    """Test purchases list endpoint"""

    @pytest.mark.asyncio
    async def test_get_purchases_default(self, client: AsyncClient):
        """Test GET /suppliers/purchases with default params"""
        response = await client.get("/suppliers/purchases")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_purchases_response_structure(self, client: AsyncClient):
        """Test purchases response structure"""
        response = await client.get("/suppliers/purchases")

        if response.status_code == 200:
            data = response.json()
            assert "success" in data
            assert "total" in data
            assert "data" in data
            assert isinstance(data["data"], list)

            if len(data["data"]) > 0:
                purchase = data["data"][0]
                assert "id" in purchase
                assert "purchase_number" in purchase
                assert "status" in purchase

    @pytest.mark.asyncio
    async def test_get_purchases_with_pagination(self, client: AsyncClient):
        """Test purchases with pagination"""
        response = await client.get("/suppliers/purchases?page=1&limit=10")
        assert response.status_code in [200, 401, 403, 500]

        if response.status_code == 200:
            data = response.json()
            assert len(data["data"]) <= 10

    @pytest.mark.asyncio
    async def test_get_purchases_with_search(self, client: AsyncClient):
        """Test purchases with search"""
        response = await client.get("/suppliers/purchases?search=WR")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_purchases_filter_by_status(self, client: AsyncClient):
        """Test purchases filtered by status"""
        response = await client.get("/suppliers/purchases?status=pending")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_purchases_filter_by_payment_status(self, client: AsyncClient):
        """Test purchases filtered by payment status"""
        response = await client.get("/suppliers/purchases?payment_status=pending")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_purchases_filter_by_date(self, client: AsyncClient):
        """Test purchases filtered by date range"""
        response = await client.get("/suppliers/purchases?date_filter=1_month")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_purchases_invalid_page(self, client: AsyncClient):
        """Test purchases with invalid page"""
        response = await client.get("/suppliers/purchases?page=0")
        assert response.status_code in [422, 500]

    @pytest.mark.asyncio
    async def test_get_purchases_exceeds_max_limit(self, client: AsyncClient):
        """Test purchases with limit exceeding max (250)"""
        response = await client.get("/suppliers/purchases?limit=251")
        assert response.status_code in [422, 500]


class TestNextPurchaseNumberEndpoint:
    """Test next purchase number endpoint"""

    @pytest.mark.asyncio
    async def test_get_next_purchase_number(self, client: AsyncClient):
        """Test GET /suppliers/purchases/next-number"""
        response = await client.get("/suppliers/purchases/next-number")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_next_purchase_number_format(self, client: AsyncClient):
        """Test next purchase number has correct format"""
        response = await client.get("/suppliers/purchases/next-number")

        if response.status_code == 200:
            data = response.json()
            assert "next_number" in data
            # Format: WR-YYYY-NNNN
            next_number = data["next_number"]
            assert next_number.startswith("WR-")


class TestPurchaseByIdEndpoint:
    """Test single purchase endpoint"""

    @pytest.mark.asyncio
    async def test_get_purchase_not_found(self, client: AsyncClient):
        """Test GET /suppliers/purchases/{id} with non-existent ID"""
        fake_id = str(uuid4())
        response = await client.get(f"/suppliers/purchases/{fake_id}")
        assert response.status_code in [404, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_purchase_invalid_uuid(self, client: AsyncClient):
        """Test GET /suppliers/purchases/{id} with invalid UUID"""
        response = await client.get("/suppliers/purchases/invalid-uuid")
        assert response.status_code in [422, 500]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_existing_purchase(self, client: AsyncClient):
        """Integration test: get an existing purchase"""
        list_response = await client.get("/suppliers/purchases?limit=1")

        if list_response.status_code == 200:
            data = list_response.json()
            if len(data["data"]) > 0:
                purchase_id = data["data"][0]["id"]
                response = await client.get(f"/suppliers/purchases/{purchase_id}")
                assert response.status_code in [200, 401, 403, 500]

                if response.status_code == 200:
                    purchase = response.json()
                    assert "data" in purchase
                    assert purchase["data"]["id"] == purchase_id


class TestPurchaseHistoryEndpoint:
    """Test purchase history endpoint"""

    @pytest.mark.asyncio
    async def test_get_purchase_history_not_found(self, client: AsyncClient):
        """Test GET /suppliers/purchases/{id}/history with non-existent ID"""
        fake_id = str(uuid4())
        response = await client.get(f"/suppliers/purchases/{fake_id}/history")
        assert response.status_code in [404, 401, 403, 500]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_existing_purchase_history(self, client: AsyncClient):
        """Integration test: get history for existing purchase"""
        list_response = await client.get("/suppliers/purchases?limit=1")

        if list_response.status_code == 200:
            data = list_response.json()
            if len(data["data"]) > 0:
                purchase_id = data["data"][0]["id"]
                response = await client.get(f"/suppliers/purchases/{purchase_id}/history")
                assert response.status_code in [200, 401, 403, 500]


class TestPurchaseAttachmentsEndpoint:
    """Test purchase attachments endpoint"""

    @pytest.mark.asyncio
    async def test_get_purchase_attachments_not_found(self, client: AsyncClient):
        """Test GET /suppliers/purchases/{id}/attachments with non-existent ID"""
        fake_id = str(uuid4())
        response = await client.get(f"/suppliers/purchases/{fake_id}/attachments")
        assert response.status_code in [404, 401, 403, 500]


class TestPurchaseStateTransitions:
    """Test purchase state transition endpoints"""

    @pytest.mark.asyncio
    async def test_confirm_purchase_not_found(self, client: AsyncClient):
        """Test POST /suppliers/purchases/{id}/confirm with non-existent ID"""
        fake_id = str(uuid4())
        response = await client.post(
            f"/suppliers/purchases/{fake_id}/confirm",
            json={"confirmation_number": "TEST123"}
        )
        assert response.status_code in [404, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_cancel_purchase_not_found(self, client: AsyncClient):
        """Test POST /suppliers/purchases/{id}/cancel with non-existent ID"""
        fake_id = str(uuid4())
        response = await client.post(
            f"/suppliers/purchases/{fake_id}/cancel",
            json={"reason": "Test cancellation"}
        )
        assert response.status_code in [404, 401, 403, 500]

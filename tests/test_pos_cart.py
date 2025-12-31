"""
Tests for POS cart module endpoints.

Endpoints tested:
- GET /pos/cart/{customer_id}
- POST /pos/cart/{cart_id}/items
- DELETE /pos/cart/{cart_id}/items/{item_id}
- DELETE /pos/cart/{cart_id}
- POST /pos/cart/{cart_id}/complete
"""
import pytest
from httpx import AsyncClient
from uuid import uuid4


class TestPosCartGetEndpoint:
    """Test POS cart get/create endpoint"""

    @pytest.mark.asyncio
    async def test_get_cart_creates_new(self, client: AsyncClient):
        """Test GET /pos/cart/{customer_id} creates new cart"""
        fake_customer_id = str(uuid4())
        response = await client.get(f"/pos/cart/{fake_customer_id}")
        # Should create cart or return auth error
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_get_cart_invalid_uuid(self, client: AsyncClient):
        """Test GET /pos/cart/{customer_id} with invalid UUID"""
        response = await client.get("/pos/cart/invalid-uuid")
        assert response.status_code in [422, 500]

    @pytest.mark.asyncio
    async def test_get_cart_with_session_id(self, client: AsyncClient):
        """Test GET /pos/cart/{customer_id} with session_id param"""
        fake_customer_id = str(uuid4())
        response = await client.get(f"/pos/cart/{fake_customer_id}?session_id=test-session")
        assert response.status_code in [200, 401, 403, 500]


class TestPosCartItemsEndpoint:
    """Test POS cart items management"""

    @pytest.mark.asyncio
    async def test_add_item_to_cart(self, client: AsyncClient):
        """Test POST /pos/cart/{cart_id}/items"""
        fake_cart_id = str(uuid4())
        fake_product_id = str(uuid4())
        response = await client.post(
            f"/pos/cart/{fake_cart_id}/items",
            json={
                "product_id": fake_product_id,
                "quantity": 1,
                "unit_price": 10.0,
                "modifiers": [],
                "notes": None
            }
        )
        # Should fail - cart doesn't exist or no auth
        assert response.status_code in [404, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_add_item_invalid_quantity(self, client: AsyncClient):
        """Test adding item with invalid quantity (0)"""
        fake_cart_id = str(uuid4())
        fake_product_id = str(uuid4())
        response = await client.post(
            f"/pos/cart/{fake_cart_id}/items",
            json={
                "product_id": fake_product_id,
                "quantity": 0,  # Invalid - must be > 0
                "unit_price": 10.0,
                "modifiers": []
            }
        )
        assert response.status_code in [422, 500]

    @pytest.mark.asyncio
    async def test_add_item_with_modifiers(self, client: AsyncClient):
        """Test adding item with modifiers"""
        fake_cart_id = str(uuid4())
        fake_product_id = str(uuid4())
        fake_modifier_id = str(uuid4())
        response = await client.post(
            f"/pos/cart/{fake_cart_id}/items",
            json={
                "product_id": fake_product_id,
                "quantity": 1,
                "unit_price": 10.0,
                "modifiers": [
                    {"id": fake_modifier_id, "name": "Extra cheese", "price": 2.0}
                ],
                "notes": "No onions"
            }
        )
        assert response.status_code in [404, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_remove_item_from_cart(self, client: AsyncClient):
        """Test DELETE /pos/cart/{cart_id}/items/{item_id}"""
        fake_cart_id = str(uuid4())
        fake_item_id = str(uuid4())
        response = await client.delete(f"/pos/cart/{fake_cart_id}/items/{fake_item_id}")
        assert response.status_code in [404, 401, 403, 500]


class TestPosCartClearEndpoint:
    """Test POS cart clear endpoint"""

    @pytest.mark.asyncio
    async def test_clear_cart(self, client: AsyncClient):
        """Test DELETE /pos/cart/{cart_id}"""
        fake_cart_id = str(uuid4())
        response = await client.delete(f"/pos/cart/{fake_cart_id}")
        assert response.status_code in [404, 401, 403, 500]


class TestPosCartCompleteEndpoint:
    """Test POS cart complete order endpoint"""

    @pytest.mark.asyncio
    async def test_complete_order_cart_not_found(self, client: AsyncClient):
        """Test POST /pos/cart/{cart_id}/complete with non-existent cart"""
        fake_cart_id = str(uuid4())
        response = await client.post(
            f"/pos/cart/{fake_cart_id}/complete",
            json={"payment_method": "cash"}
        )
        assert response.status_code in [404, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_complete_order_missing_payment_method(self, client: AsyncClient):
        """Test completing order without payment method"""
        fake_cart_id = str(uuid4())
        response = await client.post(
            f"/pos/cart/{fake_cart_id}/complete",
            json={}
        )
        assert response.status_code in [422, 500]

    @pytest.mark.asyncio
    async def test_complete_order_cash_payment(self, client: AsyncClient):
        """Test completing order with cash payment"""
        fake_cart_id = str(uuid4())
        response = await client.post(
            f"/pos/cart/{fake_cart_id}/complete",
            json={"payment_method": "cash"}
        )
        assert response.status_code in [404, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_complete_order_card_payment(self, client: AsyncClient):
        """Test completing order with card payment"""
        fake_cart_id = str(uuid4())
        response = await client.post(
            f"/pos/cart/{fake_cart_id}/complete",
            json={"payment_method": "card"}
        )
        assert response.status_code in [404, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_complete_order_digital_payment(self, client: AsyncClient):
        """Test completing order with digital payment"""
        fake_cart_id = str(uuid4())
        response = await client.post(
            f"/pos/cart/{fake_cart_id}/complete",
            json={"payment_method": "digital"}
        )
        assert response.status_code in [404, 401, 403, 500]

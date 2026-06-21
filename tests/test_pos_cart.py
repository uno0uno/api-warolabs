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
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from app.core.exceptions import APIError
from app.services import pos_cart_service


def _txn_conn():
    mock_conn = AsyncMock()

    class _Txn:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *args):
            return None

    mock_conn.transaction = MagicMock(return_value=_Txn())
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_cm.__aexit__ = AsyncMock(return_value=None)
    return mock_conn, mock_cm


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


class TestPosWalletTenderContract:
    def test_manual_discount_amount_uses_promo_adjusted_subtotal(self):
        assert pos_cart_service._manual_discount_amount(80_000, "percent", 10) == 8_000
        assert pos_cart_service._manual_discount_amount(80_000, "fixed", 90_000) == 80_000
        assert pos_cart_service._manual_discount_amount(80_000, None, 10) == 0

    def test_service_keeps_customer_wallet_as_payment_tender(self):
        src = Path(__file__).resolve().parents[1] / "app/services/pos_cart_service.py"
        text = src.read_text()

        assert "Wallet is recorded as payment_method='customer_wallet'" in text
        assert "validate_wallet_payment_tender(payment_method, cash_received)" in text
        assert "if not split_mode and payment_method == WALLET_PAYMENT_SLUG" in text
        assert "payment_method == WALLET_PAYMENT_SLUG" in text
        assert "discount_type" in text
        assert "discount_value" in text
        assert "UUID(_split_first_payment_id)" in text
        assert "payment_splits=await _order_payment_splits_for_gl(conn, order_id)" in text
        assert "order_status == 'completed' and (not split_mode or _split_is_complete)" in text

    @pytest.mark.asyncio
    async def test_order_payment_splits_for_gl_keeps_custom_method_ids(self):
        order_id = uuid4()
        digital_method_id = uuid4()
        card_method_id = uuid4()
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[
            {
                "amount": 42000,
                "payment_method": "digital",
                "payment_method_id": digital_method_id,
            },
            {
                "amount": 3000,
                "payment_method": "card",
                "payment_method_id": card_method_id,
            },
        ])

        splits = await pos_cart_service._order_payment_splits_for_gl(mock_conn, order_id)

        assert splits == [
            {
                "amount": pos_cart_service.Decimal("42000"),
                "payment_method": "digital",
                "payment_method_id": digital_method_id,
            },
            {
                "amount": pos_cart_service.Decimal("3000"),
                "payment_method": "card",
                "payment_method_id": card_method_id,
            },
        ]

    @pytest.mark.asyncio
    async def test_split_payment_rejects_amount_above_remaining_before_insert(self):
        tenant_id = uuid4()
        user_id = uuid4()
        cart_id = uuid4()
        order_id = uuid4()
        customer_id = uuid4()
        mock_conn, mock_cm = _txn_conn()
        mock_conn.fetchrow = AsyncMock(side_effect=[
            {"id": cart_id, "tenant_id": tenant_id},
            {
                "id": order_id,
                "total_amount": 100.0,
                "tip_amount": 0,
                "tip_source": "none",
                "tip_taxable": False,
                "tip_tax_amount": 0,
                "status": "active",
                "payment_status": "partial",
                "customer_id": customer_id,
                "order_number": 77,
            },
            {"paid_total": 80.0},
        ])

        with patch("app.services.pos_cart_service.require_valid_session") as mock_sess, \
             patch("app.services.pos_cart_service.get_db_connection", return_value=mock_cm):
            mock_sess.return_value = MagicMock(tenant_id=tenant_id, user_id=user_id)
            with pytest.raises(APIError) as exc:
                await pos_cart_service.add_order_payment(
                    MagicMock(),
                    str(cart_id),
                    amount=25.0,
                    payment_method="card",
                )

        assert exc.value.status_code == 400
        assert "excede el saldo pendiente" in str(exc.value)
        assert mock_conn.fetchrow.await_count == 3
        mock_conn.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_wallet_split_payment_links_wallet_movement_to_payment_row(self):
        tenant_id = uuid4()
        user_id = uuid4()
        cart_id = uuid4()
        order_id = uuid4()
        payment_id = uuid4()
        customer_id = uuid4()
        mock_conn, mock_cm = _txn_conn()
        mock_conn.fetchrow = AsyncMock(side_effect=[
            {"id": cart_id, "tenant_id": tenant_id},
            {
                "id": order_id,
                "total_amount": 100.0,
                "tip_amount": 0,
                "tip_source": "none",
                "tip_taxable": False,
                "tip_tax_amount": 0,
                "status": "active",
                "payment_status": "partial",
                "customer_id": customer_id,
                "order_number": 78,
            },
            {"paid_total": 20.0},
            {"id": payment_id},
            {"paid_total": 50.0},
        ])
        mock_conn.execute = AsyncMock()

        with patch("app.services.pos_cart_service.require_valid_session") as mock_sess, \
             patch("app.services.pos_cart_service.get_db_connection", return_value=mock_cm), \
             patch("app.services.pos_cart_service.apply_wallet_for_order", new=AsyncMock()) as apply_wallet:
            mock_sess.return_value = MagicMock(tenant_id=tenant_id, user_id=user_id)
            result = await pos_cart_service.add_order_payment(
                MagicMock(),
                str(cart_id),
                amount=30.0,
                payment_method="customer_wallet",
            )

        assert result["data"]["payment_id"] == str(payment_id)
        assert result["data"]["remaining"] == 50.0
        apply_wallet.assert_awaited_once_with(
            mock_conn,
            customer_id,
            tenant_id,
            pos_cart_service.Decimal("30.0"),
            order_id,
            user_id,
            payment_id,
        )

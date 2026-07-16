"""
Integration tests for POST /online/cart/batch endpoint.

Verifies modifier validation in create_cart_with_batch_items:
- Modifier existence and availability check
- max_qty per group enforcement
- Valid payloads succeed with correct subtotal formula

Real DB anchors (warocolombia tenant):
  tenant_id  : 93b3e582-34fa-44a6-8d0f-bf82a3608727
  product_id : ea55265f-fc65-4834-8529-0cd9cbd3950c  (Pizza Especial, price=25000)
  Groups via product_modifier_groups:
    modificador 1  (a766a3cf-...) is_required=false  max_qty=3
      Achiote/Color  64700ab3-4fda-40c6-bbec-314cbb72a762  price=3000 max_limit=1
    modificador 2  (dd5d0110-...) is_required=true  max_qty=1
      Mix de Mariscos  b04743f8-d81f-4d8b-9a6c-24b67ed8ea07  price=5000

The current fixture requires one selection from modificador 2.
"""
import pytest
from httpx import AsyncClient
from uuid import uuid4
from app.database import DatabasePool

TENANT_ID = "93b3e582-34fa-44a6-8d0f-bf82a3608727"
PRODUCT_ID = "ea55265f-fc65-4834-8529-0cd9cbd3950c"
MODIFIER_ACHIOTE = "64700ab3-4fda-40c6-bbec-314cbb72a762"   # group max_qty=3
MODIFIER_MARISCOS = "b04743f8-d81f-4d8b-9a6c-24b67ed8ea07"  # group max_qty=1


@pytest.fixture(autouse=True)
async def reset_db_pool():
    """Reset the asyncpg pool before and after each test.

    With asyncio_default_fixture_loop_scope=function, each test gets its own
    event loop. The DatabasePool singleton created on a previous loop becomes
    unusable when that loop closes. Resetting before ensures a fresh pool even
    if a test from another file left it in a bad state.
    """
    if DatabasePool._pool is not None:
        try:
            await DatabasePool._pool.close()
        except Exception:
            pass
        DatabasePool._pool = None
    yield
    if DatabasePool._pool is not None:
        try:
            await DatabasePool._pool.close()
        except Exception:
            pass
        DatabasePool._pool = None


def _batch_payload(items: list) -> dict:
    return {
        "tenant_id": TENANT_ID,
        "items": items,
        "order_type": "pickup",
    }


class TestOnlineCartBatchModifierValidation:

    @pytest.mark.asyncio
    async def test_invalid_modifier_uuid_returns_422(self, client: AsyncClient):
        """Non-existent modifier UUID must be rejected before cart is created."""
        payload = _batch_payload([{
            "product_id": PRODUCT_ID,
            "quantity": 1,
            "unit_price": 25000.0,
            "modifiers": [
                {"id": str(uuid4()), "name": "Ghost modifier", "price": 0.0}
            ],
        }])
        response = await client.post("/online/cart/batch", json=payload)
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_exceeds_group_max_qty_returns_422(self, client: AsyncClient):
        """Sending the same modifier twice violates max_qty=1 for 'modificador 2'."""
        payload = _batch_payload([{
            "product_id": PRODUCT_ID,
            "quantity": 1,
            "unit_price": 25000.0,
            "modifiers": [
                {"id": MODIFIER_MARISCOS, "name": "Mix de Mariscos", "price": 5000.0},
                {"id": MODIFIER_MARISCOS, "name": "Mix de Mariscos", "price": 5000.0},
            ],
        }])
        response = await client.post("/online/cart/batch", json=payload)
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_valid_modifier_within_max_qty_returns_200(self, client: AsyncClient):
        """Valid modifier within group limits creates the cart successfully."""
        payload = _batch_payload([{
            "product_id": PRODUCT_ID,
            "quantity": 2,
            "unit_price": 25000.0,
            "modifiers": [
                {"id": MODIFIER_ACHIOTE, "name": "Achiote/Color", "price": 3000.0},
                {"id": MODIFIER_MARISCOS, "name": "Mix de Mariscos", "price": 5000.0},
            ],
        }])
        response = await client.post("/online/cart/batch", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        # subtotal = (25000 + 3000 + 5000) * 2 = 66000
        assert data["data"]["items"][0]["subtotal"] == 66000.0

    @pytest.mark.asyncio
    async def test_modifier_quantity_above_max_limit_returns_422(self, client: AsyncClient):
        """Per-option max_limit rejects a quantity that the old flat formula accepted."""
        payload = _batch_payload([{
            "product_id": PRODUCT_ID,
            "quantity": 2,
            "unit_price": 25000.0,
            "modifiers": [
                {"id": MODIFIER_ACHIOTE, "name": "Ignored", "price": 0.0, "quantity": 2},
            ],
        }])
        response = await client.post("/online/cart/batch", json=payload)
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_no_modifiers_returns_200(self, client: AsyncClient):
        """Empty modifiers array skips validation entirely and creates cart."""
        payload = _batch_payload([{
            "product_id": PRODUCT_ID,
            "quantity": 1,
            "unit_price": 25000.0,
            "modifiers": [],
        }])
        response = await client.post("/online/cart/batch", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["data"]["items"][0]["subtotal"] == 25000.0

    @pytest.mark.asyncio
    async def test_zero_price_modifier_returns_200(self, client: AsyncClient):
        """Client-provided modifier price is ignored — backend always reads from DB.

        Achiote/Color has price=3000 in DB, so subtotal = (25000 + 3000) * 1 = 28000
        regardless of the price=0.0 sent by the client.
        """
        payload = _batch_payload([{
            "product_id": PRODUCT_ID,
            "quantity": 1,
            "unit_price": 25000.0,
            "modifiers": [
                {"id": MODIFIER_ACHIOTE, "name": "Achiote/Color", "price": 0.0},
                {"id": MODIFIER_MARISCOS, "name": "Ignored", "price": 0.0},
            ],
        }])
        response = await client.post("/online/cart/batch", json=payload)
        assert response.status_code == 200
        # subtotal = 25000 + 3000 + 5000 = 33000 (both DB prices used)
        assert response.json()["data"]["items"][0]["subtotal"] == 33000.0

    @pytest.mark.asyncio
    async def test_missing_tenant_id_returns_422(self, client: AsyncClient):
        """Malformed payload missing required tenant_id is rejected by Pydantic."""
        payload = {
            "items": [{
                "product_id": PRODUCT_ID,
                "quantity": 1,
                "unit_price": 25000.0,
                "modifiers": [],
            }]
        }
        response = await client.post("/online/cart/batch", json=payload)
        assert response.status_code == 422

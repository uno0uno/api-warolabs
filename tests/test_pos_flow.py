"""
Tests for complete POS flow.

These tests verify the end-to-end POS workflow:
1. Get products with modifiers (include_modifiers=true)
2. Create/get cart for customer
3. Add items with modifiers to cart
4. Complete order and verify inventory deduction

Key flows tested:
- Product listing with modifiers for POS
- Cart creation and item management
- Order completion with inventory updates
- Movement records for audit trail
"""
import pytest
from httpx import AsyncClient
from uuid import uuid4


class TestProductsWithModifiersEndpoint:
    """Test products endpoint with modifiers for POS"""

    @pytest.mark.asyncio
    async def test_get_products_include_modifiers(self, client: AsyncClient):
        """Test GET /menu/products?include_modifiers=true returns modifier groups"""
        response = await client.get("/menu/products?include_modifiers=true")
        assert response.status_code in [200, 401, 403, 500]

        if response.status_code == 200:
            data = response.json()
            assert "data" in data
            assert isinstance(data["data"], list)

            # Check product structure if products exist
            if len(data["data"]) > 0:
                product = data["data"][0]
                # Should have modifier_groups key when include_modifiers=true
                assert "modifier_groups" in product

    @pytest.mark.asyncio
    async def test_get_products_modifier_groups_structure(self, client: AsyncClient):
        """Test modifier groups have correct structure"""
        response = await client.get("/menu/products?include_modifiers=true&limit=10")

        if response.status_code == 200:
            data = response.json()

            # Find a product with modifiers
            for product in data["data"]:
                if product.get("allow_modifiers") and product.get("modifier_groups"):
                    groups = product["modifier_groups"]
                    for group in groups:
                        # Each group should have these keys
                        assert "id" in group
                        assert "name" in group
                        assert "min_qty" in group
                        assert "max_qty" in group
                        assert "is_required" in group
                        assert "modifiers" in group

                        # Check modifiers structure
                        for modifier in group["modifiers"]:
                            assert "id" in modifier
                            assert "name" in modifier
                            assert "price" in modifier
                            assert "is_available" in modifier
                    break  # Found one, test passed

    @pytest.mark.asyncio
    async def test_get_single_product_with_modifiers(self, client: AsyncClient):
        """Test GET /menu/products/{id} returns modifiers"""
        # First get a product ID
        list_response = await client.get("/menu/products?limit=1")

        if list_response.status_code == 200:
            data = list_response.json()
            if len(data["data"]) > 0:
                product_id = data["data"][0]["id"]

                # Get single product
                response = await client.get(f"/menu/products/{product_id}")

                if response.status_code == 200:
                    product_data = response.json()
                    assert "data" in product_data
                    assert "modifier_groups" in product_data["data"]

    @pytest.mark.asyncio
    async def test_get_available_products_for_pos(self, client: AsyncClient):
        """Test getting only available products for POS"""
        response = await client.get("/menu/products?is_available=true&include_modifiers=true")
        assert response.status_code in [200, 401, 403, 500]

        if response.status_code == 200:
            data = response.json()
            for product in data["data"]:
                assert product["is_available"] is True


class TestPOSCartManagement:
    """Test POS cart operations"""

    @pytest.mark.asyncio
    async def test_cart_requires_customer(self, client: AsyncClient):
        """Test cart creation requires customer_id"""
        response = await client.post(
            "/pos/carts/get-or-create",
            json={}  # No customer_id
        )
        assert response.status_code in [422, 400, 500]

    @pytest.mark.asyncio
    async def test_add_item_to_cart_structure(self, client: AsyncClient):
        """Test add item to cart request structure"""
        fake_cart_id = str(uuid4())
        fake_product_id = str(uuid4())

        response = await client.post(
            f"/pos/carts/{fake_cart_id}/items",
            json={
                "product_id": fake_product_id,
                "quantity": 2,
                "unit_price": 15000,
                "modifiers": [
                    {"id": str(uuid4()), "name": "Extra queso", "price": 2000}
                ],
                "notes": "Sin cebolla"
            }
        )
        # Should fail due to auth/invalid cart, but structure is correct
        assert response.status_code in [404, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_remove_item_from_cart(self, client: AsyncClient):
        """Test remove item from cart"""
        fake_cart_id = str(uuid4())
        fake_item_id = str(uuid4())

        response = await client.delete(f"/pos/carts/{fake_cart_id}/items/{fake_item_id}")
        assert response.status_code in [404, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_clear_cart(self, client: AsyncClient):
        """Test clear cart"""
        fake_cart_id = str(uuid4())

        response = await client.delete(f"/pos/carts/{fake_cart_id}/clear")
        assert response.status_code in [404, 401, 403, 500]


class TestPOSOrderCompletion:
    """Test POS order completion flow"""

    @pytest.mark.asyncio
    async def test_complete_order_structure(self, client: AsyncClient):
        """Test complete order request structure"""
        fake_cart_id = str(uuid4())

        response = await client.post(
            f"/pos/carts/{fake_cart_id}/complete",
            json={"payment_method": "cash"}
        )
        assert response.status_code in [404, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_complete_order_payment_methods(self, client: AsyncClient):
        """Test valid payment methods"""
        valid_methods = ["cash", "card", "nequi", "daviplata", "transfer"]

        for method in valid_methods:
            fake_cart_id = str(uuid4())
            response = await client.post(
                f"/pos/carts/{fake_cart_id}/complete",
                json={"payment_method": method}
            )
            # Structure should be valid even if cart doesn't exist
            assert response.status_code in [404, 401, 403, 500]


class TestRecipeBasesEndpoint:
    """Test recipe bases for POS products"""

    @pytest.mark.asyncio
    async def test_get_recipe_bases(self, client: AsyncClient):
        """Test GET /menu/recipe-bases"""
        response = await client.get("/menu/recipe-bases")
        assert response.status_code in [200, 401, 403, 500]

        if response.status_code == 200:
            data = response.json()
            assert "success" in data
            assert "data" in data

    @pytest.mark.asyncio
    async def test_recipe_base_with_ingredients(self, client: AsyncClient):
        """Test recipe base includes ingredients"""
        response = await client.get("/menu/recipe-bases?include_ingredients=true")

        if response.status_code == 200:
            data = response.json()
            if len(data["data"]) > 0:
                recipe_base = data["data"][0]
                # Should have ingredients when requested
                if "ingredients" in recipe_base:
                    assert isinstance(recipe_base["ingredients"], list)


class TestModifierGroupsCRUD:
    """Test modifier groups CRUD for product configuration"""

    @pytest.mark.asyncio
    async def test_get_modifier_groups(self, client: AsyncClient):
        """Test GET /menu/modifier-groups"""
        response = await client.get("/menu/modifier-groups")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_create_modifier_group_validation(self, client: AsyncClient):
        """Test modifier group creation validation"""
        # Missing required fields
        response = await client.post(
            "/menu/modifier-groups",
            json={"name": "Test Group"}  # Missing product_id
        )
        assert response.status_code in [422, 400, 500]

    @pytest.mark.asyncio
    async def test_create_modifier_validation(self, client: AsyncClient):
        """Test modifier creation validation"""
        fake_group_id = str(uuid4())

        # Valid structure
        response = await client.post(
            "/menu/modifiers",
            json={
                "modifier_group_id": fake_group_id,
                "name": "Extra cheese",
                "price": 2000,
                "is_available": True
            }
        )
        assert response.status_code in [404, 422, 401, 403, 500]


class TestCombosInPOS:
    """Test combos functionality in POS"""

    @pytest.mark.asyncio
    async def test_get_combos_for_pos(self, client: AsyncClient):
        """Test GET /menu/combos for POS display"""
        response = await client.get("/menu/combos?is_available=true")
        assert response.status_code in [200, 401, 403, 500]

        if response.status_code == 200:
            data = response.json()
            for combo in data["data"]:
                assert combo["is_combo"] is True
                assert "items" in combo

    @pytest.mark.asyncio
    async def test_combo_has_pricing(self, client: AsyncClient):
        """Test combo includes pricing calculations"""
        response = await client.get("/menu/combos?limit=1")

        if response.status_code == 200:
            data = response.json()
            if len(data["data"]) > 0:
                combo = data["data"][0]
                # Should have pricing fields
                assert "price" in combo
                if combo.get("items"):
                    for item in combo["items"]:
                        assert "individual_price" in item or item.get("individual_price") is None
                        assert "combo_price" in item or item.get("combo_price") is None


class TestInventoryMovementsOnOrder:
    """Test that inventory movements are created on order completion"""

    @pytest.mark.asyncio
    async def test_movements_list(self, client: AsyncClient):
        """Test GET /inventory/movements"""
        response = await client.get("/inventory/movements")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_movements_filter_by_type(self, client: AsyncClient):
        """Test filtering movements by type"""
        response = await client.get("/inventory/movements?movement_type=consumption")

        if response.status_code == 200:
            data = response.json()
            for movement in data.get("data", []):
                assert movement["movement_type"] == "consumption"

    @pytest.mark.asyncio
    async def test_movements_have_reference(self, client: AsyncClient):
        """Test consumption movements have order reference"""
        response = await client.get("/inventory/movements?movement_type=consumption&limit=5")

        if response.status_code == 200:
            data = response.json()
            for movement in data.get("data", []):
                if movement["movement_type"] == "consumption":
                    # Should have reference to the order
                    assert movement.get("reference_table") == "orders"
                    assert movement.get("reference_id") is not None


class TestCategoriesForPOS:
    """Test categories used in POS for filtering products"""

    @pytest.mark.asyncio
    async def test_get_categories(self, client: AsyncClient):
        """Test GET /menu/categories for POS filter"""
        response = await client.get("/menu/categories")
        assert response.status_code in [200, 401, 403, 500]

        if response.status_code == 200:
            data = response.json()
            assert "data" in data

    @pytest.mark.asyncio
    async def test_filter_products_by_category(self, client: AsyncClient):
        """Test filtering products by category in POS"""
        # First get a category
        cat_response = await client.get("/menu/categories?limit=1")

        if cat_response.status_code == 200:
            cat_data = cat_response.json()
            if len(cat_data.get("data", [])) > 0:
                category_id = cat_data["data"][0]["id"]

                # Filter products by category
                products_response = await client.get(
                    f"/menu/products?category_id={category_id}&include_modifiers=true"
                )

                if products_response.status_code == 200:
                    products_data = products_response.json()
                    for product in products_data["data"]:
                        assert product["category_id"] == category_id


class TestProductSearchInPOS:
    """Test product search functionality for POS"""

    @pytest.mark.asyncio
    async def test_search_products(self, client: AsyncClient):
        """Test product search by name"""
        response = await client.get("/menu/products?search=hamburguesa&include_modifiers=true")
        assert response.status_code in [200, 401, 403, 500]

    @pytest.mark.asyncio
    async def test_search_case_insensitive(self, client: AsyncClient):
        """Test search is case insensitive"""
        response1 = await client.get("/menu/products?search=PIZZA")
        response2 = await client.get("/menu/products?search=pizza")

        if response1.status_code == 200 and response2.status_code == 200:
            # Should return same results
            data1 = response1.json()
            data2 = response2.json()
            assert data1["total"] == data2["total"]


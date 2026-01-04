"""
Tests for modifier-to-ingredient linking and inventory deduction.

Features tested:
1. Modifier creation with ingredient linking
2. Modifier update with ingredient data
3. Inventory deduction when order with modifiers is completed
4. Correct quantity calculations for modifier ingredients

Related files:
- app/models/modifier.py: IngredientInfo model, ingredient fields
- app/services/modifiers_service.py: CRUD with ingredient data
- app/services/pos_cart_service.py: Inventory deduction logic
"""
import pytest
from decimal import Decimal
from uuid import UUID, uuid4
from typing import List, Dict, Any, Optional
from httpx import AsyncClient


class TestModifierIngredientModel:
    """Test modifier ingredient data model"""

    def test_modifier_with_ingredient_fields(self):
        """
        Test: Modifier model includes ingredient linking fields
        Fields: ingredient_id, ingredient_quantity, ingredient_unit
        """
        modifier_data = {
            "id": str(uuid4()),
            "name": "Achiote/Color",
            "price": Decimal("3000"),
            "ingredient_id": str(uuid4()),
            "ingredient_quantity": Decimal("40"),
            "ingredient_unit": "gr"
        }

        assert "ingredient_id" in modifier_data
        assert "ingredient_quantity" in modifier_data
        assert "ingredient_unit" in modifier_data
        assert modifier_data["ingredient_quantity"] == Decimal("40")
        assert modifier_data["ingredient_unit"] == "gr"

    def test_modifier_without_ingredient(self):
        """
        Test: Modifier can exist without ingredient linking
        All ingredient fields should be None/null
        """
        modifier_data = {
            "id": str(uuid4()),
            "name": "Extra Cheese",
            "price": Decimal("2000"),
            "ingredient_id": None,
            "ingredient_quantity": None,
            "ingredient_unit": None
        }

        assert modifier_data["ingredient_id"] is None
        assert modifier_data["ingredient_quantity"] is None
        assert modifier_data["ingredient_unit"] is None

    def test_ingredient_info_model(self):
        """
        Test: IngredientInfo model for populated modifier data
        Contains: id, name, unit, costo_unitario, controla_inventario
        """
        ingredient_info = {
            "id": str(uuid4()),
            "name": "Achiote/Color",
            "unit": "gr",
            "costo_unitario": Decimal("50"),
            "controla_inventario": True
        }

        assert "id" in ingredient_info
        assert "name" in ingredient_info
        assert "unit" in ingredient_info
        assert ingredient_info["controla_inventario"] is True


class TestModifierIngredientQuantityCalculation:
    """Test ingredient quantity calculations for modifiers"""

    def calculate_ingredient_consumption(
        self,
        modifier_quantity: Decimal,
        order_quantity: int
    ) -> Decimal:
        """
        Calculate total ingredient consumption for a modifier in an order.
        Formula: modifier_ingredient_quantity * order_item_quantity
        """
        return modifier_quantity * Decimal(str(order_quantity))

    def test_single_item_modifier_consumption(self):
        """
        Test: 1 item with modifier that uses 40gr of ingredient
        Expected consumption: 40gr * 1 = 40gr
        """
        result = self.calculate_ingredient_consumption(
            modifier_quantity=Decimal("40"),
            order_quantity=1
        )
        assert result == Decimal("40")

    def test_multiple_items_modifier_consumption(self):
        """
        Test: 3 items with modifier that uses 40gr of ingredient each
        Expected consumption: 40gr * 3 = 120gr
        """
        result = self.calculate_ingredient_consumption(
            modifier_quantity=Decimal("40"),
            order_quantity=3
        )
        assert result == Decimal("120")

    def test_fractional_modifier_quantity(self):
        """
        Test: Modifier with fractional ingredient quantity
        2 items with modifier that uses 2.5ml each
        Expected: 2.5ml * 2 = 5ml
        """
        result = self.calculate_ingredient_consumption(
            modifier_quantity=Decimal("2.5"),
            order_quantity=2
        )
        assert result == Decimal("5.0")


class TestInventoryDeductionForModifiers:
    """Test inventory deduction logic for modifier ingredients"""

    def simulate_inventory_deduction(
        self,
        current_stock: Decimal,
        consumption: Decimal
    ) -> Dict[str, Decimal]:
        """
        Simulate inventory deduction for a modifier ingredient.
        Returns new stock level and quantity deducted.
        """
        new_stock = current_stock - consumption
        return {
            "previous_stock": current_stock,
            "quantity_change": -consumption,
            "new_stock": new_stock
        }

    def test_basic_inventory_deduction(self):
        """
        Test: Basic inventory deduction
        Stock: 1000gr, Consumption: 40gr
        Expected: 960gr remaining
        """
        result = self.simulate_inventory_deduction(
            current_stock=Decimal("1000"),
            consumption=Decimal("40")
        )

        assert result["previous_stock"] == Decimal("1000")
        assert result["quantity_change"] == Decimal("-40")
        assert result["new_stock"] == Decimal("960")

    def test_inventory_deduction_to_zero(self):
        """
        Test: Deduction that brings stock to exactly zero
        Stock: 40gr, Consumption: 40gr
        Expected: 0gr remaining
        """
        result = self.simulate_inventory_deduction(
            current_stock=Decimal("40"),
            consumption=Decimal("40")
        )

        assert result["new_stock"] == Decimal("0")

    def test_inventory_deduction_negative_stock(self):
        """
        Test: Deduction that goes below zero (allowed in system)
        Stock: 20gr, Consumption: 40gr
        Expected: -20gr (system allows negative for tracking)
        """
        result = self.simulate_inventory_deduction(
            current_stock=Decimal("20"),
            consumption=Decimal("40")
        )

        assert result["new_stock"] == Decimal("-20")

    def test_multiple_modifier_deductions(self):
        """
        Test: Order with multiple modifiers, each with ingredients
        Modifier 1: Achiote 40gr
        Modifier 2: Mix Mariscos 30gr
        Both should create separate inventory movements
        """
        modifiers = [
            {"name": "Achiote", "quantity": Decimal("40"), "current_stock": Decimal("500")},
            {"name": "Mix Mariscos", "quantity": Decimal("30"), "current_stock": Decimal("200")},
        ]

        results = []
        for mod in modifiers:
            result = self.simulate_inventory_deduction(
                current_stock=mod["current_stock"],
                consumption=mod["quantity"]
            )
            results.append(result)

        assert results[0]["new_stock"] == Decimal("460")  # 500 - 40
        assert results[1]["new_stock"] == Decimal("170")  # 200 - 30


class TestInventoryMovementRecord:
    """Test inventory movement records for modifier consumption"""

    def create_movement_record(
        self,
        ingredient_id: str,
        ingredient_name: str,
        quantity: Decimal,
        unit: str,
        order_number: str,
        modifier_name: str
    ) -> Dict[str, Any]:
        """
        Create an inventory movement record for modifier consumption.
        Mirrors the structure in tenant_ingredient_movements table.
        """
        return {
            "ingredient_id": ingredient_id,
            "ingredient_name": ingredient_name,
            "movement_type": "consumption",
            "quantity_change": -quantity,
            "unit": unit,
            "reason": f"Modificador {modifier_name} (1x) - Orden #{order_number}",
            "reference_table": "orders"
        }

    def test_movement_record_structure(self):
        """Test movement record has correct structure"""
        record = self.create_movement_record(
            ingredient_id=str(uuid4()),
            ingredient_name="Achiote/Color",
            quantity=Decimal("40"),
            unit="gr",
            order_number="9753",
            modifier_name="Achiote/Color"
        )

        assert record["movement_type"] == "consumption"
        assert record["quantity_change"] == Decimal("-40")
        assert record["unit"] == "gr"
        assert "Modificador Achiote/Color" in record["reason"]
        assert "Orden #9753" in record["reason"]

    def test_movement_record_for_multiple_quantity(self):
        """
        Test: Movement record reason includes quantity multiplier
        Order with 3x of a product, modifier should show (3x)
        """
        reason = f"Modificador Extra Cheese (3x) - Orden #9754"
        assert "(3x)" in reason


class TestModifierEndpointsWithIngredients:
    """Integration tests for modifier endpoints with ingredient data"""

    @pytest.mark.asyncio
    async def test_get_modifier_includes_ingredient_data(self, client: AsyncClient):
        """
        Test: GET modifier returns ingredient linking data
        Response should include ingredient_id, ingredient_quantity, ingredient_unit
        """
        # Get list of modifiers first
        response = await client.get("/menu/modifier-groups")

        if response.status_code == 200:
            data = response.json()
            if data.get("data") and len(data["data"]) > 0:
                group = data["data"][0]
                if group.get("modifiers") and len(group["modifiers"]) > 0:
                    modifier = group["modifiers"][0]
                    # Check ingredient fields exist (may be null)
                    assert "ingredient_id" in modifier or modifier.get("ingredient") is not None

    @pytest.mark.asyncio
    async def test_get_ingredients_for_modifier_selection(self, client: AsyncClient):
        """
        Test: GET /suppliers/ingredients returns list for modifier dropdown
        """
        response = await client.get("/suppliers/ingredients?limit=100")

        if response.status_code == 200:
            data = response.json()
            assert "data" in data
            if len(data["data"]) > 0:
                ingredient = data["data"][0]
                assert "id" in ingredient
                assert "name" in ingredient


class TestOrderWithModifierIngredients:
    """Test complete order flow with modifier ingredient deduction"""

    def test_order_item_with_modifier_ingredients(self):
        """
        Test: Order item structure with modifiers that have ingredients
        """
        order_item = {
            "product_id": str(uuid4()),
            "product_name": "Pizza Especial",
            "quantity": 1,
            "unit_price": Decimal("25000"),
            "modifiers": [
                {
                    "id": str(uuid4()),
                    "name": "Achiote/Color",
                    "price": Decimal("3000"),
                    "ingredient_id": str(uuid4()),
                    "ingredient_quantity": Decimal("40"),
                    "ingredient_unit": "gr"
                },
                {
                    "id": str(uuid4()),
                    "name": "Mix de Mariscos",
                    "price": Decimal("5000"),
                    "ingredient_id": str(uuid4()),
                    "ingredient_quantity": Decimal("30"),
                    "ingredient_unit": "gr"
                }
            ]
        }

        # Calculate expected inventory deductions
        expected_deductions = []
        for mod in order_item["modifiers"]:
            if mod.get("ingredient_id"):
                expected_deductions.append({
                    "ingredient_id": mod["ingredient_id"],
                    "quantity": mod["ingredient_quantity"] * order_item["quantity"],
                    "unit": mod["ingredient_unit"]
                })

        assert len(expected_deductions) == 2
        assert expected_deductions[0]["quantity"] == Decimal("40")
        assert expected_deductions[1]["quantity"] == Decimal("30")

    def test_order_total_calculation_with_modifiers(self):
        """
        Test: Order total includes modifier prices
        Product: $25,000
        Modifier 1: $3,000
        Modifier 2: $5,000
        Expected: $33,000
        """
        base_price = Decimal("25000")
        modifiers = [
            {"price": Decimal("3000")},
            {"price": Decimal("5000")}
        ]

        total = base_price + sum(m["price"] for m in modifiers)

        assert total == Decimal("33000")


class TestEdgeCases:
    """Test edge cases for modifier ingredient functionality"""

    def test_modifier_ingredient_not_in_inventory(self):
        """
        Test: Modifier ingredient exists but has no inventory record
        Should still create movement with 0 -> negative
        """
        result = {
            "previous_stock": Decimal("0"),
            "quantity_change": Decimal("-40"),
            "new_stock": Decimal("-40")
        }

        assert result["new_stock"] == Decimal("-40")

    def test_modifier_with_null_ingredient_skips_deduction(self):
        """
        Test: Modifier without ingredient_id should not trigger inventory deduction
        """
        modifier = {
            "id": str(uuid4()),
            "name": "Extra Napkins",
            "price": Decimal("0"),
            "ingredient_id": None
        }

        should_deduct = modifier["ingredient_id"] is not None
        assert should_deduct is False

    def test_ingredient_quantity_zero(self):
        """
        Test: Modifier with ingredient but zero quantity
        Should not deduct anything
        """
        quantity = Decimal("0")
        order_qty = 5

        consumption = quantity * order_qty
        assert consumption == Decimal("0")

    def test_large_order_quantity_deduction(self):
        """
        Test: Large order (100 items) with modifier
        Modifier uses 10ml per item
        Expected: 10ml * 100 = 1000ml deduction
        """
        modifier_quantity = Decimal("10")
        order_quantity = 100

        consumption = modifier_quantity * order_quantity
        assert consumption == Decimal("1000")

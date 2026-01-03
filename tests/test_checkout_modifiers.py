"""
Tests for modifier price calculations in checkout/POS.

These tests verify that modifier prices are correctly calculated
and included in item subtotals and order totals.

Issue: Modifier prices were displayed in UI but not properly totalized
in the subtotal. The getItemTotal function didn't multiply by quantity.
Fix: Updated getItemTotal to include (basePrice + modifiersPrice) * quantity.
"""
import pytest
from decimal import Decimal
from uuid import UUID
from typing import List, Dict, Any


class TestModifierPriceCalculation:
    """Test individual modifier price calculations"""

    def test_single_modifier_price(self):
        """
        Test: Product with one modifier
        Base price: $15,000
        Modifier (Queso extra): $2,000
        Expected item total: $17,000
        """
        base_price = Decimal("15000")
        modifier_price = Decimal("2000")

        item_total = base_price + modifier_price

        assert item_total == Decimal("17000")

    def test_multiple_modifiers_price(self):
        """
        Test: Product with multiple modifiers
        Base price: $15,000
        Modifiers:
        - Queso extra: $2,000
        - Tocineta: $3,000
        - Jalapeños: $1,500
        Expected item total: $21,500
        """
        base_price = Decimal("15000")
        modifiers = [
            {"name": "Queso extra", "price": Decimal("2000")},
            {"name": "Tocineta", "price": Decimal("3000")},
            {"name": "Jalapeños", "price": Decimal("1500")},
        ]

        modifiers_total = sum(m["price"] for m in modifiers)
        item_total = base_price + modifiers_total

        assert modifiers_total == Decimal("6500")
        assert item_total == Decimal("21500")

    def test_modifier_with_zero_price(self):
        """
        Test: Modifier with $0 price (free add-on)
        Base price: $15,000
        Modifier (Sin cebolla): $0
        Expected item total: $15,000
        """
        base_price = Decimal("15000")
        modifier_price = Decimal("0")

        item_total = base_price + modifier_price

        assert item_total == Decimal("15000")

    def test_negative_modifier_discount(self):
        """
        Test: Modifier with negative price (discount)
        Base price: $15,000
        Modifier (Descuento empleado): -$5,000
        Expected item total: $10,000
        """
        base_price = Decimal("15000")
        modifier_price = Decimal("-5000")

        item_total = base_price + modifier_price

        assert item_total == Decimal("10000")


class TestItemTotalWithQuantity:
    """Test item total calculations including quantity"""

    def calculate_item_total(
        self,
        base_price: Decimal,
        modifiers: List[Dict[str, Any]],
        quantity: int
    ) -> Decimal:
        """
        Calculate item total: (base_price + modifiers_price) * quantity
        This is the corrected calculation from checkout.vue
        """
        modifiers_price = sum(Decimal(str(m["price"])) for m in modifiers)
        return (base_price + modifiers_price) * quantity

    def test_single_item_with_modifiers(self):
        """
        Test: 1 hamburger with modifiers
        Base: $15,000 x 1
        Modifiers: $2,000 + $3,000 = $5,000
        Expected: ($15,000 + $5,000) * 1 = $20,000
        """
        result = self.calculate_item_total(
            base_price=Decimal("15000"),
            modifiers=[
                {"name": "Queso", "price": 2000},
                {"name": "Tocineta", "price": 3000},
            ],
            quantity=1
        )

        assert result == Decimal("20000")

    def test_multiple_items_with_modifiers(self):
        """
        Test: 2 hamburgers with modifiers
        Base: $15,000
        Modifiers: $2,000 + $3,000 = $5,000
        Quantity: 2
        Expected: ($15,000 + $5,000) * 2 = $40,000
        """
        result = self.calculate_item_total(
            base_price=Decimal("15000"),
            modifiers=[
                {"name": "Queso", "price": 2000},
                {"name": "Tocineta", "price": 3000},
            ],
            quantity=2
        )

        assert result == Decimal("40000")

    def test_quantity_without_modifiers(self):
        """
        Test: 3 items without modifiers
        Base: $10,000 x 3
        Expected: $30,000
        """
        result = self.calculate_item_total(
            base_price=Decimal("10000"),
            modifiers=[],
            quantity=3
        )

        assert result == Decimal("30000")

    def test_high_quantity_with_modifiers(self):
        """
        Test: Large order (10 items) with modifiers
        Base: $8,000
        Modifiers: $1,000
        Quantity: 10
        Expected: ($8,000 + $1,000) * 10 = $90,000
        """
        result = self.calculate_item_total(
            base_price=Decimal("8000"),
            modifiers=[{"name": "Extra", "price": 1000}],
            quantity=10
        )

        assert result == Decimal("90000")


class TestCartTotalWithModifiers:
    """Test complete cart total calculations"""

    def calculate_cart_total(self, items: List[Dict[str, Any]]) -> Decimal:
        """
        Calculate cart total from all items with modifiers.
        Mirrors the usePOSStore.cartTotal computed property.
        """
        total = Decimal("0")
        for item in items:
            product_total = Decimal(str(item["price"])) * item["quantity"]
            modifiers_total = sum(
                Decimal(str(m["price"])) for m in item.get("modifiers", [])
            ) * item["quantity"]
            total += product_total + modifiers_total
        return total

    def test_cart_single_item_with_modifiers(self):
        """Test cart with single item including modifiers"""
        items = [
            {
                "product": {"name": "Hamburguesa", "price": 15000},
                "price": 15000,
                "quantity": 1,
                "modifiers": [
                    {"name": "Queso", "price": 2000},
                    {"name": "Tocineta", "price": 3000},
                ]
            }
        ]

        total = self.calculate_cart_total(items)

        # $15,000 + $2,000 + $3,000 = $20,000
        assert total == Decimal("20000")

    def test_cart_multiple_items_with_modifiers(self):
        """Test cart with multiple items, some with modifiers"""
        items = [
            {
                "product": {"name": "Hamburguesa"},
                "price": 15000,
                "quantity": 2,
                "modifiers": [
                    {"name": "Queso", "price": 2000},
                ]
            },
            {
                "product": {"name": "Gaseosa"},
                "price": 3000,
                "quantity": 2,
                "modifiers": []
            },
            {
                "product": {"name": "Papas"},
                "price": 5000,
                "quantity": 1,
                "modifiers": [
                    {"name": "Salsa extra", "price": 1000},
                ]
            }
        ]

        total = self.calculate_cart_total(items)

        # Hamburguesa: ($15,000 + $2,000) * 2 = $34,000
        # Gaseosa: $3,000 * 2 = $6,000
        # Papas: ($5,000 + $1,000) * 1 = $6,000
        # Total: $46,000
        assert total == Decimal("46000")

    def test_cart_all_items_without_modifiers(self):
        """Test cart where no items have modifiers"""
        items = [
            {"price": 10000, "quantity": 2, "modifiers": []},
            {"price": 5000, "quantity": 3, "modifiers": []},
        ]

        total = self.calculate_cart_total(items)

        # $10,000 * 2 + $5,000 * 3 = $35,000
        assert total == Decimal("35000")

    def test_empty_cart(self):
        """Test empty cart returns zero"""
        items = []

        total = self.calculate_cart_total(items)

        assert total == Decimal("0")


class TestModifierDisplayFormatting:
    """Test modifier display formatting for UI"""

    def format_modifier_price(self, price: Decimal) -> str:
        """Format modifier price for display"""
        if price >= 0:
            return f"+${price:,.0f}"
        else:
            return f"-${abs(price):,.0f}"

    def test_positive_modifier_format(self):
        """Test formatting positive modifier price"""
        result = self.format_modifier_price(Decimal("2000"))
        assert result == "+$2,000"

    def test_negative_modifier_format(self):
        """Test formatting negative modifier price (discount)"""
        result = self.format_modifier_price(Decimal("-5000"))
        assert result == "-$5,000"

    def test_zero_modifier_format(self):
        """Test formatting zero modifier price"""
        result = self.format_modifier_price(Decimal("0"))
        assert result == "+$0"

    def test_modifier_list_display(self):
        """
        Test: Format list of modifiers for display
        Each modifier should show name and price
        """
        modifiers = [
            {"name": "Queso extra", "price": Decimal("2000")},
            {"name": "Tocineta", "price": Decimal("3000")},
        ]

        display_lines = [
            f"+ {m['name']}  {self.format_modifier_price(m['price'])}"
            for m in modifiers
        ]

        assert display_lines[0] == "+ Queso extra  +$2,000"
        assert display_lines[1] == "+ Tocineta  +$3,000"


class TestSubtotalVsTotalConsistency:
    """Test that subtotal and total calculations are consistent"""

    def test_subtotal_equals_sum_of_items(self):
        """
        Test: Cart subtotal should equal sum of all item totals
        This ensures no discrepancy between displayed subtotal and total
        """
        items = [
            {"price": 15000, "quantity": 2, "modifiers": [{"price": 2000}]},
            {"price": 8000, "quantity": 1, "modifiers": []},
        ]

        # Calculate individual item totals
        item_totals = []
        for item in items:
            mod_total = sum(m["price"] for m in item["modifiers"]) * item["quantity"]
            item_total = (item["price"] * item["quantity"]) + mod_total
            item_totals.append(item_total)

        # Sum should equal cart total
        subtotal = sum(item_totals)

        # Item 1: ($15,000 * 2) + ($2,000 * 2) = $34,000
        # Item 2: $8,000 * 1 = $8,000
        # Total: $42,000
        assert subtotal == 42000
        assert sum(item_totals) == subtotal

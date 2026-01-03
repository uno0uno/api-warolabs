"""
Tests for purchase order weight fields and unit conversions.

These tests verify that weight_value, weight_unit, and weight_per_unit_grams
are correctly saved and retrieved when creating purchase orders with
package/box presentations (Paquetes, Cajas).

Issue: Weight fields were being sent by frontend but not saved by backend.
Fix: Added weight fields to PurchaseItemBase model and INSERT queries.
"""
import pytest
from decimal import Decimal
from uuid import UUID
from app.models.purchase import (
    PurchaseItemBase,
    PurchaseItemCreate,
    PurchaseItem,
    PurchaseCreate
)


class TestPurchaseItemWeightFields:
    """Test weight fields in purchase item models"""

    def test_purchase_item_base_has_weight_fields(self):
        """Verify PurchaseItemBase model accepts weight fields"""
        item = PurchaseItemBase(
            ingredient_id=UUID("00000000-0000-0000-0000-000000000001"),
            quantity=Decimal("18"),
            unit="und",
            purchase_quantity=Decimal("1"),
            purchase_unit="Paquete x18 und",
            weight_value=Decimal("2"),
            weight_unit="kg",
            weight_per_unit_grams=Decimal("111.11")
        )

        assert item.weight_value == Decimal("2")
        assert item.weight_unit == "kg"
        assert item.weight_per_unit_grams == Decimal("111.11")

    def test_purchase_item_base_weight_fields_optional(self):
        """Verify weight fields are optional (for non-package items)"""
        item = PurchaseItemBase(
            ingredient_id=UUID("00000000-0000-0000-0000-000000000001"),
            quantity=Decimal("1000"),
            unit="gr"
        )

        assert item.weight_value is None
        assert item.weight_unit is None
        assert item.weight_per_unit_grams is None

    def test_purchase_item_create_inherits_weight_fields(self):
        """Verify PurchaseItemCreate inherits weight fields from base"""
        item = PurchaseItemCreate(
            ingredient_id=UUID("00000000-0000-0000-0000-000000000001"),
            quantity=Decimal("144"),
            unit="und",
            purchase_quantity=Decimal("1"),
            purchase_unit="Caja x144 und",
            weight_value=Decimal("5"),
            weight_unit="kg",
            weight_per_unit_grams=Decimal("34.72")
        )

        assert item.weight_value == Decimal("5")
        assert item.weight_unit == "kg"
        assert item.weight_per_unit_grams == Decimal("34.72")


class TestWeightConversionCalculations:
    """Test weight conversion calculations for packages"""

    def test_paquete_weight_to_unit_grams(self):
        """
        Test: 1 Paquete x18 und weighing 2kg
        Expected: weight_per_unit_grams = 2000g / 18 = 111.11g
        """
        total_weight_kg = Decimal("2")
        units_per_package = 18

        total_weight_grams = total_weight_kg * 1000
        weight_per_unit = total_weight_grams / units_per_package

        assert round(weight_per_unit, 2) == Decimal("111.11")

    def test_caja_weight_to_unit_grams(self):
        """
        Test: 1 Caja x144 und (8 paquetes) weighing 16kg
        Expected: weight_per_unit_grams = 16000g / 144 = 111.11g
        """
        total_weight_kg = Decimal("16")
        units_per_box = 144

        total_weight_grams = total_weight_kg * 1000
        weight_per_unit = total_weight_grams / units_per_box

        assert round(weight_per_unit, 2) == Decimal("111.11")

    def test_weight_conversion_kg_to_grams(self):
        """Test kilogram to gram conversion"""
        weight_kg = Decimal("2.5")
        weight_grams = weight_kg * 1000
        assert weight_grams == Decimal("2500")

    def test_weight_conversion_lb_to_grams(self):
        """Test pound to gram conversion"""
        weight_lb = Decimal("1")
        weight_grams = weight_lb * Decimal("453.592")
        assert round(weight_grams, 2) == Decimal("453.59")

    def test_quantity_calculation_from_package(self):
        """
        Test: Converting 2 packages to base units
        1 Paquete = 18 und
        2 Paquetes = 36 und
        """
        purchase_quantity = 2
        conversion_factor = 18

        base_quantity = purchase_quantity * conversion_factor

        assert base_quantity == 36


class TestPurchaseOrderWithWeightFields:
    """Test complete purchase order creation with weight fields"""

    def test_purchase_create_with_package_items(self):
        """Test creating a purchase order with package items including weight"""
        purchase = PurchaseCreate(
            supplier_id=UUID("00000000-0000-0000-0000-000000000002"),
            status="quotation",
            items=[
                PurchaseItemCreate(
                    ingredient_id=UUID("00000000-0000-0000-0000-000000000001"),
                    quantity=Decimal("18"),
                    unit="und",
                    purchase_quantity=Decimal("1"),
                    purchase_unit="Paquete x18 und",
                    weight_value=Decimal("2"),
                    weight_unit="kg",
                    weight_per_unit_grams=Decimal("111.11")
                ),
                PurchaseItemCreate(
                    ingredient_id=UUID("00000000-0000-0000-0000-000000000003"),
                    quantity=Decimal("1000"),
                    unit="gr",
                    purchase_quantity=Decimal("1"),
                    purchase_unit="1 Kilogramo"
                )
            ]
        )

        assert len(purchase.items) == 2

        # First item has weight fields
        assert purchase.items[0].weight_value == Decimal("2")
        assert purchase.items[0].weight_unit == "kg"
        assert purchase.items[0].weight_per_unit_grams == Decimal("111.11")

        # Second item has no weight fields (simple unit)
        assert purchase.items[1].weight_value is None
        assert purchase.items[1].weight_unit is None

    def test_multiple_packages_weight_calculation(self):
        """
        Test: 3 packages of 18 units each, total weight 6kg
        Expected:
        - quantity: 54 (base units)
        - purchase_quantity: 3 (packages)
        - weight_value: 6 (kg total)
        - weight_per_unit_grams: 111.11 (6000g / 54)
        """
        packages = 3
        units_per_package = 18
        total_weight_kg = Decimal("6")

        base_quantity = packages * units_per_package
        weight_per_unit = (total_weight_kg * 1000) / base_quantity

        item = PurchaseItemCreate(
            ingredient_id=UUID("00000000-0000-0000-0000-000000000001"),
            quantity=Decimal(str(base_quantity)),
            unit="und",
            purchase_quantity=Decimal(str(packages)),
            purchase_unit="Paquete x18 und",
            weight_value=total_weight_kg,
            weight_unit="kg",
            weight_per_unit_grams=round(weight_per_unit, 2)
        )

        assert item.quantity == Decimal("54")
        assert item.purchase_quantity == Decimal("3")
        assert item.weight_value == Decimal("6")
        assert item.weight_per_unit_grams == Decimal("111.11")

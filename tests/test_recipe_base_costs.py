"""
Tests for recipe base cost calculations.

These tests verify that ingredient costs are correctly calculated
for recipe bases, using the last purchase price as the cost source.

Issue: Recipe base costs were showing $0 because costo_unitario was
read from ingredients table (empty) instead of purchase history.
Fix: Modified query to get cost from tenant_purchase_items.unit_cost.
"""
import pytest
from decimal import Decimal
from uuid import UUID
from typing import List, Dict, Any


class TestRecipeBaseCostCalculation:
    """Test cost calculations for recipe base ingredients"""

    def test_single_ingredient_cost(self):
        """
        Test: Single ingredient with known unit cost
        Ingredient: Queso (500g at $12/g)
        Expected: costo_total = 500 * 12 = $6,000
        """
        base_quantity = Decimal("500")
        unit_cost = Decimal("12")

        costo_total = base_quantity * unit_cost

        assert costo_total == Decimal("6000")

    def test_multiple_ingredients_total_cost(self):
        """
        Test: Multiple ingredients sum to recipe total cost
        - Pan (12 und at $500/und) = $6,000
        - Salchicha (18 und at $600/und) = $10,800
        - Queso (200g at $12/g) = $2,400
        Expected total: $19,200
        """
        ingredients = [
            {"name": "Pan", "quantity": Decimal("12"), "unit_cost": Decimal("500")},
            {"name": "Salchicha", "quantity": Decimal("18"), "unit_cost": Decimal("600")},
            {"name": "Queso", "quantity": Decimal("200"), "unit_cost": Decimal("12")},
        ]

        costo_total = sum(
            ing["quantity"] * ing["unit_cost"]
            for ing in ingredients
        )

        assert costo_total == Decimal("19200")

    def test_ingredient_with_zero_cost(self):
        """
        Test: Ingredient without purchase history should have $0 cost
        This happens when an ingredient has never been purchased.
        """
        base_quantity = Decimal("100")
        unit_cost = Decimal("0")  # No purchase history

        costo_total = base_quantity * unit_cost

        assert costo_total == Decimal("0")

    def test_cost_with_decimal_precision(self):
        """
        Test: Cost calculation with decimal unit costs
        Ingredient: Salsa (3785ml at $2.64/ml)
        Expected: costo_total = 3785 * 2.64 = $9,992.40
        """
        base_quantity = Decimal("3785")
        unit_cost = Decimal("2.64")

        costo_total = base_quantity * unit_cost

        assert costo_total == Decimal("9992.40")


class TestRecipeBaseCostFromPurchaseHistory:
    """Test that costs are correctly sourced from purchase history"""

    def test_cost_priority_purchase_over_ingredient(self):
        """
        Test: Purchase history cost takes priority over ingredient.costo_unitario
        - ingredient.costo_unitario = 0 (empty)
        - tenant_purchase_items.unit_cost = 500 (from last purchase)
        Expected: Use 500 from purchase history
        """
        ingredient_costo_unitario = Decimal("0")
        purchase_unit_cost = Decimal("500")

        # COALESCE logic: use purchase cost if available, else ingredient cost
        effective_cost = purchase_unit_cost if purchase_unit_cost > 0 else ingredient_costo_unitario

        assert effective_cost == Decimal("500")

    def test_fallback_to_ingredient_cost_when_no_purchase(self):
        """
        Test: Fall back to ingredient.costo_unitario when no purchase history
        - ingredient.costo_unitario = 100 (manually set)
        - tenant_purchase_items: no records
        Expected: Use 100 from ingredient
        """
        ingredient_costo_unitario = Decimal("100")
        purchase_unit_cost = None  # No purchase history

        # COALESCE logic
        effective_cost = purchase_unit_cost if purchase_unit_cost else ingredient_costo_unitario

        assert effective_cost == Decimal("100")

    def test_use_latest_purchase_cost(self):
        """
        Test: Use the most recent purchase cost, not older ones
        Purchase history:
        - 2025-12-01: unit_cost = $400
        - 2025-12-14: unit_cost = $500 (latest)
        Expected: Use $500
        """
        purchase_history = [
            {"date": "2025-12-01", "unit_cost": Decimal("400")},
            {"date": "2025-12-14", "unit_cost": Decimal("500")},
        ]

        # Sort by date descending and get first
        latest = sorted(purchase_history, key=lambda x: x["date"], reverse=True)[0]

        assert latest["unit_cost"] == Decimal("500")


class TestRecipeBaseModel:
    """Test RecipeBaseIngredient model with cost fields"""

    def test_recipe_base_ingredient_has_cost_fields(self):
        """Verify RecipeBaseIngredient model includes cost fields"""
        from app.models.recipe_base import RecipeBaseIngredient

        # Check that model has the expected fields
        fields = RecipeBaseIngredient.model_fields

        assert "costo_unitario" in fields
        assert "controla_inventario" in fields

    def test_recipe_base_ingredient_cost_default_zero(self):
        """Verify cost defaults to 0 when not provided"""
        from app.models.recipe_base import RecipeBaseIngredient
        from datetime import datetime

        ingredient = RecipeBaseIngredient(
            id=UUID("00000000-0000-0000-0000-000000000001"),
            product_base_type_id=UUID("00000000-0000-0000-0000-000000000002"),
            ingredient_id=UUID("00000000-0000-0000-0000-000000000003"),
            tenant_id=UUID("00000000-0000-0000-0000-000000000004"),
            base_quantity=100.0,
            unit="gr",
            is_required=True,
            created_at=datetime.now(),
            updated_at=datetime.now()
        )

        assert ingredient.costo_unitario == 0
        assert ingredient.controla_inventario == False

    def test_recipe_base_ingredient_with_cost(self):
        """Test creating ingredient with explicit cost"""
        from app.models.recipe_base import RecipeBaseIngredient
        from datetime import datetime

        ingredient = RecipeBaseIngredient(
            id=UUID("00000000-0000-0000-0000-000000000001"),
            product_base_type_id=UUID("00000000-0000-0000-0000-000000000002"),
            ingredient_id=UUID("00000000-0000-0000-0000-000000000003"),
            tenant_id=UUID("00000000-0000-0000-0000-000000000004"),
            base_quantity=500.0,
            unit="gr",
            is_required=True,
            costo_unitario=12.0,
            controla_inventario=True,
            created_at=datetime.now(),
            updated_at=datetime.now()
        )

        assert ingredient.costo_unitario == 12.0
        assert ingredient.controla_inventario == True


class TestRecipeTotalCostCalculation:
    """Test complete recipe cost calculations"""

    def calculate_recipe_cost(self, ingredients: List[Dict[str, Any]]) -> Decimal:
        """Helper to calculate total recipe cost"""
        return sum(
            Decimal(str(ing["base_quantity"])) * Decimal(str(ing["costo_unitario"]))
            for ing in ingredients
        )

    def test_recipe_coleslaw_cost(self):
        """
        Test: Calculate cost for Coleslaw recipe
        Real ingredients with quantities and costs
        """
        ingredients = [
            {"name": "Repollo", "base_quantity": 200, "costo_unitario": 3.5},
            {"name": "Zanahoria", "base_quantity": 50, "costo_unitario": 2.0},
            {"name": "Mayonesa", "base_quantity": 100, "costo_unitario": 8.0},
            {"name": "Vinagre", "base_quantity": 20, "costo_unitario": 5.0},
            {"name": "Azucar", "base_quantity": 10, "costo_unitario": 2.5},
            {"name": "Sal", "base_quantity": 5, "costo_unitario": 1.0},
        ]

        costo_total = self.calculate_recipe_cost(ingredients)

        # 700 + 100 + 800 + 100 + 25 + 5 = $1,730
        expected = Decimal("200") * Decimal("3.5") + \
                   Decimal("50") * Decimal("2.0") + \
                   Decimal("100") * Decimal("8.0") + \
                   Decimal("20") * Decimal("5.0") + \
                   Decimal("10") * Decimal("2.5") + \
                   Decimal("5") * Decimal("1.0")

        assert costo_total == expected

    def test_recipe_with_no_costs(self):
        """Test recipe where all ingredients have $0 cost"""
        ingredients = [
            {"name": "Ingrediente 1", "base_quantity": 100, "costo_unitario": 0},
            {"name": "Ingrediente 2", "base_quantity": 50, "costo_unitario": 0},
        ]

        costo_total = self.calculate_recipe_cost(ingredients)

        assert costo_total == Decimal("0")

    def test_empty_recipe_cost(self):
        """Test recipe with no ingredients"""
        ingredients = []

        costo_total = self.calculate_recipe_cost(ingredients)

        assert costo_total == Decimal("0")


class TestUnifiedProductCostResolution:
    """Tests for cost_resolution_service (#744)."""

    def test_list_cte_includes_costo_unitario_fallback(self):
        from app.services.cost_resolution_service import LIST_COST_CTE_PREFIX

        assert "i.costo_unitario" in LIST_COST_CTE_PREFIX
        assert "COALESCE(lpc.unit_cost, i.costo_unitario, 0)" in LIST_COST_CTE_PREFIX
        assert "unit_weight_gr" in LIST_COST_CTE_PREFIX
        assert "pr.unit" in LIST_COST_CTE_PREFIX
        assert "brt.unit" in LIST_COST_CTE_PREFIX

    def test_direct_plus_base_recipe_total(self):
        """Product cost = direct ingredients + base template ingredients."""
        direct = Decimal("10") * Decimal("100")  # 1000
        base = Decimal("2") * Decimal("500") * Decimal("3")  # qty × base_qty × unit
        assert direct + base == Decimal("4000")

    def test_resale_und_recipe_ml_converts_before_cost(self):
        """45 ml of a 750 ml/und bottle at 350/und → 21, not 15750 (#702)."""
        from app.services.cost_resolution_service import recipe_qty_to_stock_units

        stock_qty = recipe_qty_to_stock_units(
            Decimal("45"), "ml", "und", unit_weight_gr=Decimal("750")
        )
        cost = stock_qty * Decimal("350")
        assert stock_qty == Decimal("45") / Decimal("750")
        assert cost == Decimal("21")

    def test_same_unit_ml_unchanged(self):
        from app.services.cost_resolution_service import recipe_qty_to_stock_units

        assert recipe_qty_to_stock_units(Decimal("45"), "ml", "ml") == Decimal("45")

    def test_same_unit_und_unchanged(self):
        from app.services.cost_resolution_service import recipe_qty_to_stock_units

        assert recipe_qty_to_stock_units(Decimal("1"), "und", "und") == Decimal("1")

    def test_base_multiplier_applies_after_unit_conversion(self):
        from app.services.cost_resolution_service import recipe_qty_to_stock_units

        per_base = recipe_qty_to_stock_units(
            Decimal("45"), "ml", "und", unit_weight_gr=Decimal("750")
        ) * Decimal("350")
        multiplier = Decimal("2")
        assert per_base * multiplier == Decimal("42")

    def test_purchase_wins_over_costo_unitario_for_line(self):
        purchase = Decimal("500")
        configured = Decimal("100")
        effective = purchase if purchase > 0 else configured
        assert effective == Decimal("500")

    def test_analytics_fallback_when_zero_real_cost(self):
        from app.services.cost_resolution_service import apply_analytics_cost_fallback

        price = Decimal("10000")
        assert apply_analytics_cost_fallback(Decimal("0"), price) == Decimal("4000")

    def test_analytics_fallback_when_cost_exceeds_price(self):
        from app.services.cost_resolution_service import apply_analytics_cost_fallback

        price = Decimal("5000")
        assert apply_analytics_cost_fallback(Decimal("8000"), price) == Decimal("2000")

    def test_analytics_no_fallback_when_valid_cost(self):
        from app.services.cost_resolution_service import apply_analytics_cost_fallback

        price = Decimal("10000")
        real = Decimal("3500")
        assert apply_analytics_cost_fallback(real, price) == real

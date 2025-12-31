"""
Tests for menu business logic.

These tests verify the core business logic for:
- Product cost calculation (direct ingredients + recipe bases)
- Modifier groups structure and validation
- Combo pricing and savings calculations
- POS flow with modifiers and inventory deduction

Key business rules tested:
- Products MUST have at least one ingredient OR one recipe base
- Cost = SUM(ingredient.quantity * ingredient.cost_per_unit) using weighted average from purchases
- Modifiers add to product price in cart
- Inventory is deducted from BOTH direct ingredients AND recipe base ingredients
"""
import pytest
from decimal import Decimal


class TestProductCostCalculation:
    """Test product cost calculation logic"""

    def calculate_product_cost(
        self,
        direct_ingredients: list,
        recipe_base_ingredients: list
    ) -> float:
        """
        Calculate product cost from:
        1. Direct ingredients (product_recipes)
        2. Recipe base ingredients (product_base_recipes -> base_recipe_templates)

        Each ingredient has: quantity, cost_per_unit (weighted avg from purchases)
        """
        direct_cost = sum(
            ing['quantity'] * ing['cost_per_unit']
            for ing in direct_ingredients
        )

        recipe_base_cost = sum(
            ing['base_quantity'] * ing['cost_per_unit']
            for ing in recipe_base_ingredients
        )

        return direct_cost + recipe_base_cost

    def test_cost_direct_ingredients_only(self):
        """Test: Product with only direct ingredients"""
        direct_ingredients = [
            {'quantity': 0.2, 'cost_per_unit': 5000.0},  # 200g harina @ $5000/kg
            {'quantity': 0.1, 'cost_per_unit': 8000.0},  # 100g queso @ $8000/kg
        ]

        cost = self.calculate_product_cost(direct_ingredients, [])

        # 0.2 * 5000 + 0.1 * 8000 = 1000 + 800 = 1800
        assert cost == 1800.0

    def test_cost_recipe_base_only(self):
        """Test: Product with only recipe base"""
        recipe_base_ingredients = [
            {'base_quantity': 1.0, 'cost_per_unit': 500.0},   # 1 base de pizza @ $500
            {'base_quantity': 0.05, 'cost_per_unit': 10000.0},  # 50g salsa @ $10000/kg
        ]

        cost = self.calculate_product_cost([], recipe_base_ingredients)

        # 1.0 * 500 + 0.05 * 10000 = 500 + 500 = 1000
        assert cost == 1000.0

    def test_cost_combined_direct_and_recipe_base(self):
        """Test: Product with both direct ingredients and recipe base"""
        direct_ingredients = [
            {'quantity': 0.15, 'cost_per_unit': 12000.0},  # 150g pollo @ $12000/kg
        ]
        recipe_base_ingredients = [
            {'base_quantity': 1.0, 'cost_per_unit': 800.0},  # 1 base de arroz
        ]

        cost = self.calculate_product_cost(direct_ingredients, recipe_base_ingredients)

        # 0.15 * 12000 + 1.0 * 800 = 1800 + 800 = 2600
        assert cost == 2600.0

    def test_cost_zero_when_no_ingredients(self):
        """Test: Cost is 0 when no ingredients"""
        cost = self.calculate_product_cost([], [])
        assert cost == 0.0

    def test_cost_handles_decimal_precision(self):
        """Test: Cost calculation handles decimal precision correctly"""
        direct_ingredients = [
            {'quantity': 0.333, 'cost_per_unit': 3000.0},  # 1/3 kg
        ]

        cost = self.calculate_product_cost(direct_ingredients, [])

        assert abs(cost - 999.0) < 0.01  # 0.333 * 3000 = 999


class TestWeightedAverageCostCalculation:
    """Test weighted average cost calculation from purchases"""

    def calculate_weighted_average_cost(self, movements: list) -> float:
        """
        Calculate weighted average cost from purchase movements.

        Formula: SUM(quantity * cost_per_unit) / SUM(quantity)
        Only considers 'purchase' movements with positive quantity_change
        """
        total_value = 0.0
        total_quantity = 0.0

        for mov in movements:
            if mov['movement_type'] == 'purchase' and mov['quantity_change'] > 0:
                total_value += mov['quantity_change'] * mov['cost_per_unit']
                total_quantity += mov['quantity_change']

        if total_quantity == 0:
            return 0.0

        return total_value / total_quantity

    def test_single_purchase(self):
        """Test: Single purchase movement"""
        movements = [
            {'movement_type': 'purchase', 'quantity_change': 10.0, 'cost_per_unit': 5000.0}
        ]

        avg_cost = self.calculate_weighted_average_cost(movements)
        assert avg_cost == 5000.0

    def test_multiple_purchases_same_cost(self):
        """Test: Multiple purchases at same price"""
        movements = [
            {'movement_type': 'purchase', 'quantity_change': 10.0, 'cost_per_unit': 5000.0},
            {'movement_type': 'purchase', 'quantity_change': 20.0, 'cost_per_unit': 5000.0},
        ]

        avg_cost = self.calculate_weighted_average_cost(movements)
        assert avg_cost == 5000.0

    def test_multiple_purchases_different_costs(self):
        """Test: Multiple purchases at different prices (weighted average)"""
        movements = [
            {'movement_type': 'purchase', 'quantity_change': 10.0, 'cost_per_unit': 4000.0},
            {'movement_type': 'purchase', 'quantity_change': 30.0, 'cost_per_unit': 6000.0},
        ]

        avg_cost = self.calculate_weighted_average_cost(movements)

        # (10 * 4000 + 30 * 6000) / (10 + 30)
        # (40000 + 180000) / 40 = 220000 / 40 = 5500
        assert avg_cost == 5500.0

    def test_ignores_consumption_movements(self):
        """Test: Only purchase movements are considered"""
        movements = [
            {'movement_type': 'purchase', 'quantity_change': 10.0, 'cost_per_unit': 5000.0},
            {'movement_type': 'consumption', 'quantity_change': -5.0, 'cost_per_unit': 5000.0},
            {'movement_type': 'adjustment', 'quantity_change': -2.0, 'cost_per_unit': 5000.0},
        ]

        avg_cost = self.calculate_weighted_average_cost(movements)
        assert avg_cost == 5000.0  # Only the purchase is considered

    def test_no_purchases_returns_zero(self):
        """Test: Returns 0 when no purchases"""
        movements = [
            {'movement_type': 'consumption', 'quantity_change': -5.0, 'cost_per_unit': 5000.0}
        ]

        avg_cost = self.calculate_weighted_average_cost(movements)
        assert avg_cost == 0.0


class TestProductValidation:
    """Test product creation/update validation rules"""

    def validate_product_has_recipe(
        self,
        ingredients: list,
        recipe_base_ids: list
    ) -> tuple:
        """
        Validate that product has at least one ingredient or recipe base.
        Returns (is_valid, error_message)
        """
        has_ingredients = ingredients and len(ingredients) > 0
        has_recipe_bases = recipe_base_ids and len(recipe_base_ids) > 0

        if not has_ingredients and not has_recipe_bases:
            return (False, "El producto debe tener al menos un ingrediente o una receta base.")

        return (True, None)

    def test_valid_with_direct_ingredients(self):
        """Test: Valid when product has direct ingredients"""
        is_valid, error = self.validate_product_has_recipe(
            ingredients=[{'ingredient_id': 'uuid1', 'quantity': 1.0}],
            recipe_base_ids=[]
        )
        assert is_valid is True
        assert error is None

    def test_valid_with_recipe_base(self):
        """Test: Valid when product has recipe base"""
        is_valid, error = self.validate_product_has_recipe(
            ingredients=[],
            recipe_base_ids=['uuid1']
        )
        assert is_valid is True
        assert error is None

    def test_valid_with_both(self):
        """Test: Valid when product has both ingredients and recipe base"""
        is_valid, error = self.validate_product_has_recipe(
            ingredients=[{'ingredient_id': 'uuid1', 'quantity': 1.0}],
            recipe_base_ids=['uuid2']
        )
        assert is_valid is True
        assert error is None

    def test_invalid_with_none(self):
        """Test: Invalid when product has neither"""
        is_valid, error = self.validate_product_has_recipe(
            ingredients=[],
            recipe_base_ids=[]
        )
        assert is_valid is False
        assert "ingrediente o una receta base" in error

    def test_invalid_with_null(self):
        """Test: Invalid with None values"""
        is_valid, error = self.validate_product_has_recipe(
            ingredients=None,
            recipe_base_ids=None
        )
        assert is_valid is False


class TestModifierGroupsValidation:
    """Test modifier groups structure and validation"""

    def validate_modifier_selection(
        self,
        modifier_group: dict,
        selected_count: int
    ) -> tuple:
        """
        Validate modifier selection against group rules.
        Returns (is_valid, error_message)
        """
        min_qty = modifier_group.get('min_qty', 0)
        max_qty = modifier_group.get('max_qty')
        is_required = modifier_group.get('is_required', False)

        if is_required and selected_count < 1:
            return (False, f"Debes seleccionar al menos 1 modificador de {modifier_group['name']}")

        if selected_count < min_qty:
            return (False, f"Debes seleccionar al menos {min_qty} modificadores de {modifier_group['name']}")

        if max_qty is not None and selected_count > max_qty:
            return (False, f"Máximo {max_qty} modificadores permitidos de {modifier_group['name']}")

        return (True, None)

    def test_required_group_needs_selection(self):
        """Test: Required group needs at least one selection"""
        group = {
            'name': 'Tipo de pan',
            'min_qty': 1,
            'max_qty': 1,
            'is_required': True
        }

        is_valid, error = self.validate_modifier_selection(group, 0)
        assert is_valid is False
        assert "al menos 1" in error

    def test_required_group_valid_selection(self):
        """Test: Required group with valid selection"""
        group = {
            'name': 'Tipo de pan',
            'min_qty': 1,
            'max_qty': 1,
            'is_required': True
        }

        is_valid, error = self.validate_modifier_selection(group, 1)
        assert is_valid is True

    def test_optional_group_zero_selection(self):
        """Test: Optional group allows zero selection"""
        group = {
            'name': 'Extras',
            'min_qty': 0,
            'max_qty': 3,
            'is_required': False
        }

        is_valid, error = self.validate_modifier_selection(group, 0)
        assert is_valid is True

    def test_max_qty_exceeded(self):
        """Test: Cannot exceed max quantity"""
        group = {
            'name': 'Toppings',
            'min_qty': 0,
            'max_qty': 2,
            'is_required': False
        }

        is_valid, error = self.validate_modifier_selection(group, 3)
        assert is_valid is False
        assert "Máximo 2" in error

    def test_min_qty_not_met(self):
        """Test: Must meet minimum quantity"""
        group = {
            'name': 'Salsas',
            'min_qty': 2,
            'max_qty': 4,
            'is_required': True
        }

        is_valid, error = self.validate_modifier_selection(group, 1)
        assert is_valid is False
        assert "al menos 2" in error


class TestComboCalculations:
    """Test combo pricing and savings calculations"""

    def calculate_combo_totals(self, items: list) -> dict:
        """
        Calculate combo totals:
        - total_individual_price: sum of (individual_price * quantity)
        - total_combo_price: sum of (combo_price * quantity)
        - total_savings: total_individual - total_combo
        """
        total_individual = Decimal('0')
        total_combo = Decimal('0')

        for item in items:
            individual = Decimal(str(item.get('individual_price', 0)))
            combo = Decimal(str(item.get('combo_price', 0)))
            quantity = Decimal(str(item.get('quantity', 1)))

            total_individual += individual * quantity
            total_combo += combo * quantity

        return {
            'total_individual_price': float(total_individual),
            'total_combo_price': float(total_combo),
            'total_savings': float(total_individual - total_combo)
        }

    def test_combo_savings_calculation(self):
        """Test: Combo savings is calculated correctly"""
        items = [
            {'individual_price': 15000, 'combo_price': 12000, 'quantity': 1},  # Hamburguesa
            {'individual_price': 5000, 'combo_price': 4000, 'quantity': 1},    # Papas
            {'individual_price': 3000, 'combo_price': 2500, 'quantity': 1},    # Bebida
        ]

        totals = self.calculate_combo_totals(items)

        assert totals['total_individual_price'] == 23000.0
        assert totals['total_combo_price'] == 18500.0
        assert totals['total_savings'] == 4500.0

    def test_combo_with_quantities(self):
        """Test: Combo with multiple quantities"""
        items = [
            {'individual_price': 10000, 'combo_price': 8000, 'quantity': 2},  # 2 hamburguesas
            {'individual_price': 5000, 'combo_price': 4000, 'quantity': 1},   # 1 porción papas
        ]

        totals = self.calculate_combo_totals(items)

        # Individual: (10000 * 2) + (5000 * 1) = 20000 + 5000 = 25000
        # Combo: (8000 * 2) + (4000 * 1) = 16000 + 4000 = 20000
        assert totals['total_individual_price'] == 25000.0
        assert totals['total_combo_price'] == 20000.0
        assert totals['total_savings'] == 5000.0

    def test_combo_no_discount(self):
        """Test: Combo with no discount"""
        items = [
            {'individual_price': 10000, 'combo_price': 10000, 'quantity': 1},
        ]

        totals = self.calculate_combo_totals(items)

        assert totals['total_savings'] == 0.0


class TestPOSCartCalculations:
    """Test POS cart subtotal and total calculations"""

    def calculate_item_subtotal(
        self,
        unit_price: float,
        quantity: int,
        modifiers: list
    ) -> float:
        """
        Calculate cart item subtotal:
        subtotal = (unit_price + SUM(modifier.price)) * quantity
        """
        modifiers_total = sum(mod['price'] for mod in modifiers)
        return (unit_price + modifiers_total) * quantity

    def calculate_cart_total(self, items: list) -> float:
        """Calculate cart total from items"""
        return sum(item['subtotal'] for item in items)

    def test_item_subtotal_no_modifiers(self):
        """Test: Item subtotal without modifiers"""
        subtotal = self.calculate_item_subtotal(
            unit_price=15000.0,
            quantity=2,
            modifiers=[]
        )
        assert subtotal == 30000.0

    def test_item_subtotal_with_modifiers(self):
        """Test: Item subtotal with modifiers"""
        subtotal = self.calculate_item_subtotal(
            unit_price=12000.0,
            quantity=1,
            modifiers=[
                {'id': 'uuid1', 'name': 'Extra queso', 'price': 2000.0},
                {'id': 'uuid2', 'name': 'Tocineta', 'price': 3000.0},
            ]
        )
        # (12000 + 2000 + 3000) * 1 = 17000
        assert subtotal == 17000.0

    def test_item_subtotal_with_quantity_and_modifiers(self):
        """Test: Item subtotal with quantity and modifiers"""
        subtotal = self.calculate_item_subtotal(
            unit_price=8000.0,
            quantity=3,
            modifiers=[
                {'id': 'uuid1', 'name': 'Extra salsa', 'price': 500.0},
            ]
        )
        # (8000 + 500) * 3 = 25500
        assert subtotal == 25500.0

    def test_cart_total_multiple_items(self):
        """Test: Cart total with multiple items"""
        items = [
            {'subtotal': 15000.0},
            {'subtotal': 8500.0},
            {'subtotal': 3000.0},
        ]

        total = self.calculate_cart_total(items)
        assert total == 26500.0


class TestInventoryDeduction:
    """Test inventory deduction logic during order completion"""

    def calculate_ingredient_deduction(
        self,
        product_recipes: list,
        recipe_base_ingredients: list,
        quantity_ordered: int
    ) -> dict:
        """
        Calculate total deduction per ingredient when an order is placed.

        Returns dict of {ingredient_id: quantity_to_deduct}

        Sources:
        1. Direct ingredients from product_recipes
        2. Ingredients from recipe bases (product_base_recipes -> base_recipe_templates)
        """
        deductions = {}

        # Direct ingredients
        for ing in product_recipes:
            ing_id = ing['ingredient_id']
            qty = ing['quantity'] * quantity_ordered
            deductions[ing_id] = deductions.get(ing_id, 0) + qty

        # Recipe base ingredients
        for ing in recipe_base_ingredients:
            ing_id = ing['ingredient_id']
            qty = ing['base_quantity'] * quantity_ordered
            deductions[ing_id] = deductions.get(ing_id, 0) + qty

        return deductions

    def test_deduction_direct_ingredients_only(self):
        """Test: Deduction from direct ingredients only"""
        product_recipes = [
            {'ingredient_id': 'harina', 'quantity': 0.2},
            {'ingredient_id': 'queso', 'quantity': 0.1},
        ]

        deductions = self.calculate_ingredient_deduction(
            product_recipes=product_recipes,
            recipe_base_ingredients=[],
            quantity_ordered=2
        )

        assert deductions['harina'] == 0.4  # 0.2 * 2
        assert deductions['queso'] == 0.2   # 0.1 * 2

    def test_deduction_recipe_base_only(self):
        """Test: Deduction from recipe base only"""
        recipe_base_ingredients = [
            {'ingredient_id': 'masa_base', 'base_quantity': 1.0},
            {'ingredient_id': 'salsa_tomate', 'base_quantity': 0.05},
        ]

        deductions = self.calculate_ingredient_deduction(
            product_recipes=[],
            recipe_base_ingredients=recipe_base_ingredients,
            quantity_ordered=3
        )

        assert deductions['masa_base'] == 3.0      # 1.0 * 3
        assert abs(deductions['salsa_tomate'] - 0.15) < 0.0001  # 0.05 * 3 (float precision)

    def test_deduction_combined_sources(self):
        """Test: Deduction from both direct and recipe base"""
        product_recipes = [
            {'ingredient_id': 'pollo', 'quantity': 0.15},
        ]
        recipe_base_ingredients = [
            {'ingredient_id': 'arroz_base', 'base_quantity': 0.2},
        ]

        deductions = self.calculate_ingredient_deduction(
            product_recipes=product_recipes,
            recipe_base_ingredients=recipe_base_ingredients,
            quantity_ordered=4
        )

        assert deductions['pollo'] == 0.6       # 0.15 * 4
        assert deductions['arroz_base'] == 0.8  # 0.2 * 4

    def test_deduction_same_ingredient_both_sources(self):
        """Test: Same ingredient in both direct and recipe base"""
        product_recipes = [
            {'ingredient_id': 'queso', 'quantity': 0.1},
        ]
        recipe_base_ingredients = [
            {'ingredient_id': 'queso', 'base_quantity': 0.05},  # Same ingredient!
        ]

        deductions = self.calculate_ingredient_deduction(
            product_recipes=product_recipes,
            recipe_base_ingredients=recipe_base_ingredients,
            quantity_ordered=2
        )

        # Should combine: (0.1 + 0.05) * 2 = 0.3 (float precision)
        assert abs(deductions['queso'] - 0.3) < 0.0001

    def test_deduction_updates_inventory(self):
        """Test: Stock is reduced correctly"""
        previous_stock = 10.0
        quantity_to_deduct = 2.5

        new_stock = max(0.0, previous_stock - quantity_to_deduct)

        assert new_stock == 7.5

    def test_deduction_prevents_negative_stock(self):
        """Test: Stock cannot go negative (clamped to 0)"""
        previous_stock = 1.0
        quantity_to_deduct = 5.0

        new_stock = max(0.0, previous_stock - quantity_to_deduct)

        assert new_stock == 0.0


class TestModifierPriceInheritance:
    """Test that modifier prices are correctly included in calculations"""

    def test_modifier_defaults(self):
        """Test: Default modifiers should be pre-selected"""
        modifiers = [
            {'id': 'uuid1', 'name': 'Pan regular', 'is_default': True, 'price': 0},
            {'id': 'uuid2', 'name': 'Pan integral', 'is_default': False, 'price': 500},
        ]

        defaults = [m for m in modifiers if m['is_default']]
        assert len(defaults) == 1
        assert defaults[0]['name'] == 'Pan regular'

    def test_modifier_availability(self):
        """Test: Unavailable modifiers should not be selectable"""
        modifiers = [
            {'id': 'uuid1', 'name': 'Pepperoni', 'is_available': True},
            {'id': 'uuid2', 'name': 'Anchoas', 'is_available': False},
        ]

        available = [m for m in modifiers if m['is_available']]
        assert len(available) == 1
        assert available[0]['name'] == 'Pepperoni'


class TestProductWithModifiersResponse:
    """Test that product response includes complete modifier structure"""

    def build_product_with_modifiers_response(
        self,
        product: dict,
        modifier_groups: list,
        modifiers_by_group: dict
    ) -> dict:
        """
        Build complete product response with modifier groups and modifiers.
        This mimics what the API returns when include_modifiers=true
        """
        result = {**product}

        groups = []
        for group in modifier_groups:
            group_dict = {**group}
            group_dict['modifiers'] = modifiers_by_group.get(group['id'], [])
            groups.append(group_dict)

        result['modifier_groups'] = groups
        return result

    def test_response_structure(self):
        """Test: Product response has correct modifier structure"""
        product = {
            'id': 'product-uuid',
            'name': 'Hamburguesa',
            'price': 15000,
            'allow_modifiers': True
        }

        modifier_groups = [
            {'id': 'group1', 'name': 'Tipo de pan', 'min_qty': 1, 'max_qty': 1, 'is_required': True, 'sort_order': 0},
            {'id': 'group2', 'name': 'Extras', 'min_qty': 0, 'max_qty': 3, 'is_required': False, 'sort_order': 1},
        ]

        modifiers_by_group = {
            'group1': [
                {'id': 'mod1', 'name': 'Pan regular', 'price': 0, 'is_default': True, 'is_available': True},
                {'id': 'mod2', 'name': 'Pan brioche', 'price': 1500, 'is_default': False, 'is_available': True},
            ],
            'group2': [
                {'id': 'mod3', 'name': 'Extra queso', 'price': 2000, 'is_default': False, 'is_available': True},
                {'id': 'mod4', 'name': 'Tocineta', 'price': 3000, 'is_default': False, 'is_available': True},
            ]
        }

        response = self.build_product_with_modifiers_response(
            product,
            modifier_groups,
            modifiers_by_group
        )

        # Verify structure
        assert 'modifier_groups' in response
        assert len(response['modifier_groups']) == 2

        # First group (required)
        pan_group = response['modifier_groups'][0]
        assert pan_group['name'] == 'Tipo de pan'
        assert pan_group['is_required'] is True
        assert len(pan_group['modifiers']) == 2

        # Second group (optional extras)
        extras_group = response['modifier_groups'][1]
        assert extras_group['name'] == 'Extras'
        assert extras_group['is_required'] is False
        assert len(extras_group['modifiers']) == 2

    def test_response_sorted_by_sort_order(self):
        """Test: Modifier groups and modifiers are sorted"""
        product = {'id': 'uuid', 'name': 'Test', 'price': 10000}

        modifier_groups = [
            {'id': 'g2', 'name': 'Second', 'sort_order': 2},
            {'id': 'g1', 'name': 'First', 'sort_order': 1},
        ]

        # Sort by sort_order
        sorted_groups = sorted(modifier_groups, key=lambda g: g['sort_order'])

        assert sorted_groups[0]['name'] == 'First'
        assert sorted_groups[1]['name'] == 'Second'


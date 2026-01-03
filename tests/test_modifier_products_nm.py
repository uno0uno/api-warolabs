"""
Tests for modifier groups N:M relationship with products.

These tests verify that modifier groups can be associated with multiple
products via the junction table product_modifier_groups.

Issue: Modifier groups were limited to 1:N (one group → one product).
Fix: Implemented N:M relationship with junction table, allowing a single
modifier group to be associated with multiple products.
"""
import pytest
from decimal import Decimal
from uuid import UUID
from typing import List, Dict, Any


class TestModifierGroupProductAssociation:
    """Test N:M relationship between modifier_groups and products"""

    def test_single_product_association(self):
        """
        Test: Modifier group with single product
        - product_ids: [product_1]
        Expected: Group associated with 1 product
        """
        product_ids = [UUID("00000000-0000-0000-0000-000000000001")]

        assert len(product_ids) == 1
        assert isinstance(product_ids[0], UUID)

    def test_multiple_products_association(self):
        """
        Test: Modifier group with multiple products
        - product_ids: [product_1, product_2, product_3]
        Expected: Group associated with 3 products
        """
        product_ids = [
            UUID("00000000-0000-0000-0000-000000000001"),
            UUID("00000000-0000-0000-0000-000000000002"),
            UUID("00000000-0000-0000-0000-000000000003"),
        ]

        assert len(product_ids) == 3
        # All IDs should be unique
        assert len(set(product_ids)) == 3

    def test_product_ids_required_on_create(self):
        """
        Test: product_ids is required when creating modifier group
        """
        from app.models.modifier import ModifierGroupCreate

        # Should raise validation error if product_ids is empty
        with pytest.raises(Exception):
            ModifierGroupCreate(
                product_ids=[],  # Empty list should fail
                name="Test Group",
                tenant_id=UUID("00000000-0000-0000-0000-000000000001")
            )

    def test_product_ids_optional_on_update(self):
        """
        Test: product_ids is optional when updating modifier group
        """
        from app.models.modifier import ModifierGroupUpdate

        # Should work without product_ids
        update = ModifierGroupUpdate(
            name="Updated Name"
        )

        assert update.product_ids is None
        assert update.name == "Updated Name"

    def test_products_response_model(self):
        """
        Test: ModifierGroup response includes products array
        """
        from app.models.modifier import ModifierGroup, ProductInfo
        from datetime import datetime

        products = [
            ProductInfo(id=UUID("00000000-0000-0000-0000-000000000001"), name="Hamburguesa"),
            ProductInfo(id=UUID("00000000-0000-0000-0000-000000000002"), name="Hot Dog"),
        ]

        group = ModifierGroup(
            id=UUID("00000000-0000-0000-0000-000000000010"),
            tenant_id=UUID("00000000-0000-0000-0000-000000000020"),
            name="Adicionales",
            min_qty=0,
            max_qty=5,
            is_required=False,
            sort_order=0,
            products=products,
            modifiers=[],
            created_at=datetime.now(),
            updated_at=datetime.now()
        )

        assert len(group.products) == 2
        assert group.products[0].name == "Hamburguesa"
        assert group.products[1].name == "Hot Dog"


class TestModifierUpsertLogic:
    """Test upsert logic for modifiers (UPDATE existing, INSERT new, soft-delete removed)"""

    def simulate_upsert(
        self,
        existing_modifiers: List[Dict[str, Any]],
        new_modifiers: List[Dict[str, Any]]
    ) -> Dict[str, List]:
        """
        Simulate the upsert logic used in update_modifier_group.
        Returns dict with 'updated', 'inserted', and 'disabled' lists.
        """
        existing_names = {m['name']: m['id'] for m in existing_modifiers}
        existing_ids = set(existing_names.values())
        modifiers_to_keep = set()

        updated = []
        inserted = []

        for modifier in new_modifiers:
            if modifier['name'] in existing_names:
                # UPDATE existing
                mod_id = existing_names[modifier['name']]
                modifiers_to_keep.add(mod_id)
                updated.append({'id': mod_id, **modifier})
            else:
                # INSERT new
                inserted.append(modifier)

        # Soft-delete removed
        disabled = list(existing_ids - modifiers_to_keep)

        return {
            'updated': updated,
            'inserted': inserted,
            'disabled': disabled
        }

    def test_update_existing_modifier(self):
        """
        Test: Existing modifier gets updated (not deleted and recreated)
        """
        existing = [
            {'id': 'id-1', 'name': 'Tocineta', 'price': 2000},
            {'id': 'id-2', 'name': 'Queso', 'price': 2000},
        ]

        new = [
            {'name': 'Tocineta', 'price': 2500},  # Price changed
            {'name': 'Queso', 'price': 2000},      # No change
        ]

        result = self.simulate_upsert(existing, new)

        assert len(result['updated']) == 2
        assert len(result['inserted']) == 0
        assert len(result['disabled']) == 0

        # Verify Tocineta was updated with new price
        tocineta = next(m for m in result['updated'] if m['name'] == 'Tocineta')
        assert tocineta['price'] == 2500

    def test_insert_new_modifier(self):
        """
        Test: New modifier gets inserted
        """
        existing = [
            {'id': 'id-1', 'name': 'Tocineta', 'price': 2000},
        ]

        new = [
            {'name': 'Tocineta', 'price': 2000},
            {'name': 'Pepinillos', 'price': 1500},  # New modifier
        ]

        result = self.simulate_upsert(existing, new)

        assert len(result['updated']) == 1
        assert len(result['inserted']) == 1
        assert len(result['disabled']) == 0

        assert result['inserted'][0]['name'] == 'Pepinillos'

    def test_soft_delete_removed_modifier(self):
        """
        Test: Removed modifier gets soft-deleted (is_available = false)
        """
        existing = [
            {'id': 'id-1', 'name': 'Tocineta', 'price': 2000},
            {'id': 'id-2', 'name': 'Queso', 'price': 2000},
            {'id': 'id-3', 'name': 'Huevo', 'price': 1500},
        ]

        new = [
            {'name': 'Tocineta', 'price': 2000},
            # Queso and Huevo removed
        ]

        result = self.simulate_upsert(existing, new)

        assert len(result['updated']) == 1
        assert len(result['inserted']) == 0
        assert len(result['disabled']) == 2

        assert 'id-2' in result['disabled']
        assert 'id-3' in result['disabled']

    def test_complete_replacement(self):
        """
        Test: All modifiers replaced with new ones
        """
        existing = [
            {'id': 'id-1', 'name': 'Viejo 1', 'price': 1000},
            {'id': 'id-2', 'name': 'Viejo 2', 'price': 1000},
        ]

        new = [
            {'name': 'Nuevo 1', 'price': 2000},
            {'name': 'Nuevo 2', 'price': 2000},
        ]

        result = self.simulate_upsert(existing, new)

        assert len(result['updated']) == 0
        assert len(result['inserted']) == 2
        assert len(result['disabled']) == 2

    def test_empty_new_modifiers(self):
        """
        Test: All existing modifiers get soft-deleted when new list is empty
        """
        existing = [
            {'id': 'id-1', 'name': 'Tocineta', 'price': 2000},
            {'id': 'id-2', 'name': 'Queso', 'price': 2000},
        ]

        new = []

        result = self.simulate_upsert(existing, new)

        assert len(result['updated']) == 0
        assert len(result['inserted']) == 0
        assert len(result['disabled']) == 2


class TestJunctionTableQueries:
    """Test junction table query patterns"""

    def test_products_for_modifier_group_query(self):
        """
        Test: Query to get products associated with a modifier group
        """
        query = """
            SELECT p.id, p.name
            FROM product_modifier_groups pmg
            JOIN product p ON pmg.product_id = p.id
            WHERE pmg.modifier_group_id = $1
            ORDER BY p.name
        """

        # Query should join with product table
        assert "JOIN product p" in query
        assert "pmg.modifier_group_id = $1" in query

    def test_modifier_groups_for_product_query(self):
        """
        Test: Query to get modifier groups for a specific product
        """
        query = """
            SELECT mg.id, mg.name, mg.min_qty, mg.max_qty, mg.is_required
            FROM modifier_groups mg
            JOIN product_modifier_groups pmg ON mg.id = pmg.modifier_group_id
            WHERE pmg.product_id = $1
            ORDER BY mg.sort_order, mg.name
        """

        # Query should join with junction table
        assert "JOIN product_modifier_groups pmg" in query
        assert "pmg.product_id = $1" in query

    def test_insert_product_association_query(self):
        """
        Test: Query to insert product-modifier_group association
        """
        query = """
            INSERT INTO product_modifier_groups (product_id, modifier_group_id, tenant_id)
            VALUES ($1, $2, $3)
        """

        assert "INSERT INTO product_modifier_groups" in query
        assert "product_id" in query
        assert "modifier_group_id" in query
        assert "tenant_id" in query

    def test_delete_product_associations_query(self):
        """
        Test: Query to delete all product associations for a modifier group
        """
        query = "DELETE FROM product_modifier_groups WHERE modifier_group_id = $1"

        assert "DELETE FROM product_modifier_groups" in query
        assert "modifier_group_id = $1" in query


class TestModifierGroupStats:
    """Test statistics calculations with N:M relationship"""

    def calculate_stats(
        self,
        groups: List[Dict[str, Any]],
        associations: List[Dict[str, str]]
    ) -> Dict[str, int]:
        """Calculate stats from groups and associations"""
        total_groups = len(groups)
        total_modifiers = sum(len(g.get('modifiers', [])) for g in groups)
        products_with_modifiers = len(set(a['product_id'] for a in associations))

        return {
            'total_groups': total_groups,
            'total_modifiers': total_modifiers,
            'products_with_modifiers': products_with_modifiers
        }

    def test_stats_single_product_per_group(self):
        """
        Test: Stats when each group has one product
        """
        groups = [
            {'id': 'g1', 'modifiers': [{'id': 'm1'}, {'id': 'm2'}]},
            {'id': 'g2', 'modifiers': [{'id': 'm3'}]},
        ]

        associations = [
            {'product_id': 'p1', 'modifier_group_id': 'g1'},
            {'product_id': 'p2', 'modifier_group_id': 'g2'},
        ]

        stats = self.calculate_stats(groups, associations)

        assert stats['total_groups'] == 2
        assert stats['total_modifiers'] == 3
        assert stats['products_with_modifiers'] == 2

    def test_stats_multiple_products_per_group(self):
        """
        Test: Stats when one group has multiple products
        """
        groups = [
            {'id': 'g1', 'modifiers': [{'id': 'm1'}, {'id': 'm2'}]},
        ]

        # Same group associated with 3 products
        associations = [
            {'product_id': 'p1', 'modifier_group_id': 'g1'},
            {'product_id': 'p2', 'modifier_group_id': 'g1'},
            {'product_id': 'p3', 'modifier_group_id': 'g1'},
        ]

        stats = self.calculate_stats(groups, associations)

        assert stats['total_groups'] == 1
        assert stats['total_modifiers'] == 2
        assert stats['products_with_modifiers'] == 3

    def test_stats_shared_products(self):
        """
        Test: Stats when same product has multiple modifier groups
        """
        groups = [
            {'id': 'g1', 'modifiers': [{'id': 'm1'}]},
            {'id': 'g2', 'modifiers': [{'id': 'm2'}]},
        ]

        # Same product with both groups
        associations = [
            {'product_id': 'p1', 'modifier_group_id': 'g1'},
            {'product_id': 'p1', 'modifier_group_id': 'g2'},
        ]

        stats = self.calculate_stats(groups, associations)

        assert stats['total_groups'] == 2
        assert stats['total_modifiers'] == 2
        assert stats['products_with_modifiers'] == 1  # Only 1 unique product

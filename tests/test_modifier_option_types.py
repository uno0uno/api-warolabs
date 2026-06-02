"""Modifier option types — validation and ingredient line resolution (#1121)."""
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.models.modifier import ModifierCreate, ModifierRecipeLineBase
from app.services.modifier_option_service import (
    validate_modifier_option_fields,
    _merge_ingredient_lines,
    calculated_modifier_option_unit_cost,
    resolve_modifier_ingredient_lines,
)


def test_validate_ingredient_requires_ingredient_id():
    mod = ModifierCreate(name="Extra", price=0, option_type="INGREDIENT")
    with pytest.raises(ValueError, match="ingredient_id"):
        validate_modifier_option_fields(mod)


def test_validate_recipe_requires_composition():
    mod = ModifierCreate(name="Salsa", price=0, option_type="RECIPE")
    with pytest.raises(ValueError, match="recipe_base_type_id"):
        validate_modifier_option_fields(mod)


def test_validate_recipe_with_lines_ok():
    mod = ModifierCreate(
        name="Salsa",
        price=0,
        option_type="RECIPE",
        recipe_lines=[
            ModifierRecipeLineBase(
                ingredient_id=uuid4(),
                quantity=Decimal("10"),
                unit="gr",
            )
        ],
    )
    validate_modifier_option_fields(mod)


def test_validate_none_rejects_fks():
    mod = ModifierCreate(
        name="Sin inventario",
        price=1000,
        option_type="NONE",
        ingredient_id=uuid4(),
    )
    with pytest.raises(ValueError, match="NONE"):
        validate_modifier_option_fields(mod)


def test_merge_ingredient_lines_sums_same_ingredient():
    ing = uuid4()
    merged = _merge_ingredient_lines(
        [
            {"ingredient_id": ing, "quantity": 10.0, "unit": "gr"},
            {"ingredient_id": ing, "quantity": 5.0, "unit": "gr"},
        ]
    )
    assert len(merged) == 1
    assert merged[0]["quantity"] == 15.0


@pytest.mark.asyncio
async def test_resolve_ingredient_option_single_line():
    modifier_id = uuid4()
    tenant_id = uuid4()
    ing_id = uuid4()

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "id": modifier_id,
            "option_type": "INGREDIENT",
            "ingredient_id": ing_id,
            "ingredient_quantity": Decimal("2"),
            "ingredient_unit": "und",
            "recipe_base_type_id": None,
            "recipe_base_quantity": 1,
            "linked_product_id": None,
            "linked_product_quantity": 1,
            "ingredient_name": "Queso",
            "controla_inventario": True,
        }
    )
    conn.fetch = AsyncMock(return_value=[])

    with patch(
        "app.services.modifier_option_service.resolve_recipe_quantity_to_base_unit",
        new=AsyncMock(return_value=2.0),
    ):
        lines = await resolve_modifier_ingredient_lines(conn, modifier_id, tenant_id)

    assert len(lines) == 1
    assert lines[0]["ingredient_id"] == ing_id
    assert lines[0]["quantity"] == 2.0


@pytest.mark.asyncio
async def test_calculated_cost_sums_line_costs():
    modifier_id = uuid4()
    tenant_id = uuid4()
    ing_id = uuid4()

    conn = AsyncMock()

    async def fake_resolve(*_a, **_k):
        return [{"ingredient_id": ing_id, "quantity": 3.0, "unit": "und", "ingredient_name": "X"}]

    with patch(
        "app.services.modifier_option_service.resolve_modifier_ingredient_lines",
        new=fake_resolve,
    ), patch(
        "app.services.modifier_option_service._get_ingredient_unit_costs",
        new=AsyncMock(return_value={str(ing_id): Decimal("10")}),
    ):
        cost = await calculated_modifier_option_unit_cost(conn, modifier_id, tenant_id)

    assert cost == Decimal("30")

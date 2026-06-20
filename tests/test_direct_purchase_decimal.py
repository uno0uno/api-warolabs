from decimal import Decimal

from app.services.direct_purchase_service import (
    _calculate_direct_purchase_total,
    _catalog_direct_purchase_conversion_factor,
    _resolve_direct_purchase_item_decimals,
)


def test_direct_purchase_decimals_use_total_to_derive_base_unit_cost():
    item = {
        "quantity": "1.345",
        "purchase_quantity": "1.345",
        "unit_cost": 8900 / 1.345,
        "total_cost": "8900",
    }

    values = _resolve_direct_purchase_item_decimals(
        item,
        base_unit="gr",
        purchase_unit="Kilo · 1.000 gr",
        conversion_factor=Decimal("1000"),
    )

    assert values["purchase_quantity"] == Decimal("1.345000000000000")
    assert values["conversion_factor"] == Decimal("1000.000000000000000")
    assert values["base_quantity"] == Decimal("1345.000000000000000")
    assert values["item_total"] == Decimal("8900.000000000000000")
    assert values["base_unit_cost"] == Decimal("6.617100371747212")


def test_direct_purchase_total_prefers_exact_item_total():
    items = [
        {
            "quantity": "1.345",
            "purchase_quantity": "1.345",
            "unit_cost": 8900 / 1.345,
            "total_cost": "8900",
        },
        {
            "quantity": "2",
            "purchase_quantity": "2",
            "unit_cost": "1500.25",
        },
    ]

    assert _calculate_direct_purchase_total(items) == Decimal("11900.500000000000000")


def test_direct_purchase_catalog_fallback_matches_base_unit():
    ingredient = {
        "unit": "ml",
        "unit_weight_gr": None,
        "unit_weight_unit": None,
    }

    factor = _catalog_direct_purchase_conversion_factor("lt", "ml", ingredient)
    values = _resolve_direct_purchase_item_decimals(
        {
            "quantity": "1",
            "purchase_quantity": "1.345",
            "total_cost": "8900",
        },
        base_unit="ml",
        purchase_unit="lt",
        conversion_factor=factor,
    )

    assert factor == Decimal("1000.000000000000000")
    assert values["base_quantity"] == Decimal("1345.000000000000000")
    assert values["base_unit_cost"] == Decimal("6.617100371747212")


def test_direct_purchase_catalog_fallback_for_weighted_units():
    ingredient = {
        "unit": "und",
        "unit_weight_gr": "250",
        "unit_weight_unit": "ml",
    }

    factor = _catalog_direct_purchase_conversion_factor("galon", "und", ingredient)
    values = _resolve_direct_purchase_item_decimals(
        {
            "quantity": "2",
            "purchase_quantity": "2",
            "total_cost": "7570",
        },
        base_unit="und",
        purchase_unit="galon",
        conversion_factor=factor,
    )

    assert factor == Decimal("15.140000000000000")
    assert values["base_quantity"] == Decimal("30.280000000000000")
    assert values["base_unit_cost"] == Decimal("250.000000000000000")

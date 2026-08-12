"""Modifiers CSV import validation (#2257)."""
from decimal import Decimal

from app.services.menu_import_service import (
    modifiers_csv_template_bytes,
    parse_modifiers_csv,
    validate_modifier_line,
)


def test_modifiers_template_has_headers():
    text = modifiers_csv_template_bytes().decode("utf-8-sig")
    assert "group_name,option_name,price" in text.splitlines()[0]


def test_parse_modifiers_csv():
    rows = parse_modifiers_csv(modifiers_csv_template_bytes())
    assert len(rows) >= 2
    assert rows[0]["group_name"]


def test_validate_none_option_happy():
    ok, err = validate_modifier_line(
        {
            "group_name": "Cocción",
            "option_name": "Medio",
            "price": "0",
            "option_type": "NONE",
        },
        2,
    )
    assert err is None
    assert ok["option_type"] == "NONE"
    assert ok["price"] == Decimal("0")


def test_validate_ingredient_requires_fields():
    ok, err = validate_modifier_line(
        {
            "group_name": "Extras",
            "option_name": "Queso",
            "price": "2000",
            "option_type": "INGREDIENT",
            "ingredient": "",
        },
        3,
    )
    assert ok is None
    assert err["field"] == "ingredient"


def test_validate_rejects_product_type_v1():
    ok, err = validate_modifier_line(
        {
            "group_name": "G",
            "option_name": "O",
            "price": "1",
            "option_type": "PRODUCT",
        },
        4,
    )
    assert ok is None
    assert err["field"] == "option_type"


def test_validate_none_rejects_ingredient_link():
    ok, err = validate_modifier_line(
        {
            "group_name": "G",
            "option_name": "O",
            "price": "0",
            "option_type": "NONE",
            "ingredient": "Tomate",
        },
        5,
    )
    assert ok is None
    assert err["field"] == "option_type"

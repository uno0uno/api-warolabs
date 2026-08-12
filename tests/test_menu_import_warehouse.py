"""Warehouse CSV import validation (#2254)."""
from app.services.menu_import_service import (
    parse_warehouse_csv,
    validate_warehouse_row,
    warehouse_csv_template_bytes,
)


def test_warehouse_template_has_headers():
    text = warehouse_csv_template_bytes().decode("utf-8-sig")
    assert "name,unit,type" in text.splitlines()[0]
    assert "is_resale" in text


def test_parse_warehouse_csv_rows():
    raw = warehouse_csv_template_bytes()
    rows = parse_warehouse_csv(raw)
    assert len(rows) >= 2
    assert rows[0]["name"]


def test_validate_resale_requires_und_and_weight():
    ok, err = validate_warehouse_row(
        {
            "name": "Coke",
            "unit": "kg",
            "is_resale": "true",
            "unit_weight_gr": "350",
            "unit_weight_unit": "ml",
        },
        2,
    )
    assert ok is None
    assert err["field"] == "unit"


def test_validate_resale_decision_a_needs_product_without_sell_fields():
    ok, err = validate_warehouse_row(
        {
            "name": "Coke",
            "unit": "und",
            "is_resale": "true",
            "unit_weight_gr": "350",
            "unit_weight_unit": "ml",
            "create_product": "true",
            "price": "",
            "menu_category": "",
        },
        2,
    )
    assert err is None
    assert ok["needs_product"] is True
    assert ok["will_create_product"] is False


def test_validate_resale_will_create_product_with_sell_fields():
    ok, err = validate_warehouse_row(
        {
            "name": "Coke",
            "unit": "und",
            "is_resale": "true",
            "unit_weight_gr": "350",
            "unit_weight_unit": "ml",
            "create_product": "true",
            "price": "3500",
            "menu_category": "Bebidas",
        },
        2,
    )
    assert err is None
    assert ok["will_create_product"] is True
    assert ok["needs_product"] is False


def test_validate_normal_ingredient():
    ok, err = validate_warehouse_row(
        {"name": "Tomate", "unit": "kg", "type": "food", "is_resale": "false"},
        3,
    )
    assert err is None
    assert ok["is_resale"] is False
    assert ok["will_create_product"] is False

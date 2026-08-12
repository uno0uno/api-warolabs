"""Menu products CSV import validation (#2256)."""
from decimal import Decimal

from app.services.menu_import_service import (
    parse_products_csv,
    products_csv_template_bytes,
    validate_product_row,
)


def test_products_template_has_headers():
    text = products_csv_template_bytes().decode("utf-8-sig")
    assert "name,price,menu_category" in text.splitlines()[0]
    assert "finish_resale" in text


def test_parse_products_csv():
    rows = parse_products_csv(products_csv_template_bytes())
    assert len(rows) >= 2


def test_validate_menu_product_happy_path():
    ok, err = validate_product_row(
        {
            "name": "Pasta",
            "price": "18000",
            "menu_category": "Platos",
            "recipe_bases": "Salsa tomate",
            "finish_resale": "false",
        },
        2,
    )
    assert err is None
    assert ok["finish_resale"] is False
    assert ok["recipe_base_names"] == ["Salsa tomate"]
    assert ok["price"] == Decimal("18000")


def test_validate_finish_resale_rejects_recipe_bases():
    ok, err = validate_product_row(
        {
            "name": "Coke",
            "price": "3500",
            "menu_category": "Bebidas",
            "recipe_bases": "Something",
            "finish_resale": "true",
        },
        3,
    )
    assert ok is None
    assert err["field"] == "recipe_bases"


def test_validate_requires_price():
    ok, err = validate_product_row(
        {"name": "X", "price": "", "menu_category": "A", "finish_resale": "false"},
        4,
    )
    assert ok is None
    assert err["field"] == "price"


def test_validate_finish_resale_happy_shape():
    ok, err = validate_product_row(
        {
            "name": "Coca-Cola 350ml",
            "price": "3500",
            "menu_category": "Bebidas",
            "finish_resale": "true",
        },
        5,
    )
    assert err is None
    assert ok["finish_resale"] is True
    assert ok["recipe_base_names"] == []

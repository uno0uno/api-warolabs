"""Recipe bases CSV import validation (#2255)."""
from app.services.menu_import_service import (
    parse_recipe_bases_csv,
    recipe_bases_csv_template_bytes,
    validate_recipe_base_line,
)


def test_recipe_bases_template_has_headers():
    text = recipe_bases_csv_template_bytes().decode("utf-8-sig")
    assert "recipe_name,ingredient,quantity,unit" in text.splitlines()[0]


def test_parse_recipe_bases_csv_groups_lines():
    raw = recipe_bases_csv_template_bytes()
    rows = parse_recipe_bases_csv(raw)
    assert len(rows) >= 2
    assert rows[0]["recipe_name"] == rows[1]["recipe_name"]


def test_validate_recipe_line_requires_fields():
    ok, err = validate_recipe_base_line(
        {"recipe_name": "", "ingredient": "Tomate", "quantity": "1", "unit": "kg"},
        2,
    )
    assert ok is None
    assert err["field"] == "recipe_name"


def test_validate_recipe_line_happy_path():
    ok, err = validate_recipe_base_line(
        {
            "recipe_name": "Salsa",
            "ingredient": "Tomate",
            "quantity": "0.5",
            "unit": "kg",
            "notes": "fresco",
        },
        3,
    )
    assert err is None
    assert ok["quantity"] == 0.5
    assert ok["unit"] == "kg"


def test_validate_quantity_must_be_positive():
    ok, err = validate_recipe_base_line(
        {"recipe_name": "Salsa", "ingredient": "Tomate", "quantity": "0", "unit": "kg"},
        4,
    )
    assert ok is None
    assert err["field"] == "quantity"


def test_validate_accepts_ingredient_name_alias():
    ok, err = validate_recipe_base_line(
        {
            "recipe_name": "Salsa",
            "ingredient_name": "Tomate",
            "quantity": "1",
            "unit": "kg",
        },
        5,
    )
    assert err is None
    assert ok["ingredient"] == "Tomate"

from pathlib import Path
import re


MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "091_purchase_inventory_decimal_scales.sql"

HIGH_PRECISION_COLUMNS = {
    "tenant_purchase_items": [
        "quantity",
        "purchase_quantity",
        "quantity_received",
        "unit_cost",
    ],
    "tenant_inventory": [
        "current_stock",
        "minimum_stock",
        "maximum_stock",
    ],
    "tenant_ingredient_movements": [
        "quantity_change",
        "previous_stock",
        "new_stock",
        "cost_per_unit",
    ],
    "ingredient_purchase_units": [
        "conversion_factor",
    ],
}

MONEY_COLUMNS = {
    "tenant_purchase_items": ["total_cost"],
    "ingredient_purchase_units": ["unit_cost"],
}


def _sql() -> str:
    return MIGRATION.read_text()


def _assert_column_cast(sql: str, table: str, column: str, precision: str, scale: int) -> None:
    pattern = (
        rf"ALTER\s+COLUMN\s+{column}\s+TYPE\s+NUMERIC\({precision}\)"
        rf"\s+USING\s+ROUND\({column}::numeric,\s*{scale}\)"
    )
    assert re.search(pattern, sql, re.IGNORECASE), f"{table}.{column} missing expected cast"


def test_migration_exists_and_documents_issue_scope():
    sql = _sql()

    assert "warocol.com#1419" in sql
    assert "tenant_purchase_items" in sql
    assert "tenant_inventory" in sql
    assert "tenant_ingredient_movements" in sql
    assert "ingredient_purchase_units" in sql


def test_high_precision_columns_are_bounded_and_rounded():
    sql = _sql()

    for table, columns in HIGH_PRECISION_COLUMNS.items():
        assert re.search(rf"ALTER\s+TABLE\s+{table}\b", sql, re.IGNORECASE)
        for column in columns:
            _assert_column_cast(sql, table, column, "30, 15", 15)


def test_money_columns_are_currency_scaled_and_rounded():
    sql = _sql()

    for table, columns in MONEY_COLUMNS.items():
        assert re.search(rf"ALTER\s+TABLE\s+{table}\b", sql, re.IGNORECASE)
        for column in columns:
            _assert_column_cast(sql, table, column, "18, 2", 2)


def test_every_target_column_has_a_comment():
    sql = _sql()
    all_columns = {}
    for table, columns in HIGH_PRECISION_COLUMNS.items():
        all_columns.setdefault(table, []).extend(columns)
    for table, columns in MONEY_COLUMNS.items():
        all_columns.setdefault(table, []).extend(columns)

    for table, columns in all_columns.items():
        for column in columns:
            assert f"COMMENT ON COLUMN {table}.{column}" in sql

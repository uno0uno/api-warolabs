"""
Guards for warehouse-article → stock-0 inventory seeding (#716).

Entry-point matrix (all tenant creates must go through create_tenant_ingredient):
- Catálogo / IngredientePropioPanel → POST /api/suppliers/ingredients
- Recipe / product / modifier searchers → InlineCatalogCreateShell → same panel
- Compra directa → InlineCatalogCreateShell → same panel
- Resale 1:1 → products_service create + convert_to_resale → create_tenant_ingredient

Out of scope: AI/admin global ingredients (tenant_id IS NULL).
"""
from pathlib import Path

BACKFILL_SQL = Path("sql/20260724_backfill_tenant_inventory_zero.sql").read_text()
INGREDIENTS_SERVICE = Path("app/services/ingredients_service.py").read_text()
PRODUCTS_SERVICE = Path("app/services/products_service.py").read_text()


def test_backfill_sql_is_scoped_idempotent_and_non_destructive():
    assert "INSERT INTO tenant_inventory" in BACKFILL_SQL
    assert "i.tenant_id IS NOT NULL" in BACKFILL_SQL
    assert "NOT EXISTS" in BACKFILL_SQL
    assert "ON CONFLICT (tenant_id, ingredient_id) DO NOTHING" in BACKFILL_SQL
    assert "DROP" not in BACKFILL_SQL.upper()
    assert "DELETE" not in BACKFILL_SQL.upper()
    assert "current_stock" in BACKFILL_SQL and "minimum_stock" in BACKFILL_SQL


def test_create_tenant_ingredient_source_seeds_zero_inventory():
    assert "async def create_tenant_ingredient" in INGREDIENTS_SERVICE
    seed_idx = INGREDIENTS_SERVICE.index(
        "INSERT INTO tenant_inventory (tenant_id, ingredient_id, current_stock, minimum_stock)"
    )
    create_idx = INGREDIENTS_SERVICE.index("async def create_tenant_ingredient")
    update_idx = INGREDIENTS_SERVICE.index("async def update_tenant_ingredient")
    assert create_idx < seed_idx < update_idx
    assert "ON CONFLICT (tenant_id, ingredient_id) DO NOTHING" in INGREDIENTS_SERVICE[seed_idx:seed_idx + 400]


def test_auto_resale_and_convert_call_create_tenant_ingredient():
    assert "from app.services.ingredients_service import create_tenant_ingredient" in PRODUCTS_SERVICE
    assert PRODUCTS_SERVICE.count("await create_tenant_ingredient(") >= 2
    assert "async def convert_product_to_resale" in PRODUCTS_SERVICE
    convert_idx = PRODUCTS_SERVICE.index("async def convert_product_to_resale")
    assert "await create_tenant_ingredient(" in PRODUCTS_SERVICE[convert_idx:]


def test_ai_global_ingredient_insert_does_not_seed_tenant_inventory():
    """AI path inserts global ingredients (tenant_id NULL) without tenant_inventory."""
    assert "async def create_ai_ingredient" in INGREDIENTS_SERVICE
    ai_idx = INGREDIENTS_SERVICE.index("async def create_ai_ingredient")
    # No tenant_inventory insert inside the AI create function body
    next_def = INGREDIENTS_SERVICE.find("\nasync def ", ai_idx + 1)
    ai_body = INGREDIENTS_SERVICE[ai_idx:next_def if next_def != -1 else None]
    assert "VALUES ($1, $2, NULL, TRUE, NOW())" in ai_body
    assert "INSERT INTO tenant_inventory" not in ai_body

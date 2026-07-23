"""
Unified real-cost resolution for menu products (costo_calculado).

Priority for each ingredient unit cost:
  1. Latest tenant_purchase_items.unit_cost (tenant-scoped, purchase_date DESC)
  2. ingredients.costo_unitario
  3. 0

Product total = sum(direct product_recipes) + sum(product_base_recipes × base_recipe_templates).
"""
from decimal import Decimal
from typing import Optional
from uuid import UUID

# Shared CTE for list queries — $1 must be tenant_id
LIST_COST_CTE_PREFIX = """
    WITH latest_purchase_costs AS (
        SELECT DISTINCT ON (pi.ingredient_id)
            pi.ingredient_id,
            pi.unit_cost
        FROM tenant_purchase_items pi
        JOIN tenant_purchases tp ON pi.purchase_id = tp.id
        WHERE tp.tenant_id = $1
          AND pi.unit_cost IS NOT NULL
          AND pi.unit_cost > 0
        ORDER BY pi.ingredient_id, tp.purchase_date DESC
    ),
    direct_costs AS (
        SELECT
            pr.product_id,
            SUM(
                pr.quantity * COALESCE(lpc.unit_cost, i.costo_unitario, 0)
            ) AS direct_cost
        FROM product_recipes pr
        JOIN ingredients i ON pr.ingredient_id = i.id
        LEFT JOIN latest_purchase_costs lpc ON pr.ingredient_id = lpc.ingredient_id
        GROUP BY pr.product_id
    ),
    base_costs AS (
        SELECT
            pbr.product_id,
            SUM(
                pbr.quantity * brt.base_quantity * COALESCE(lpc.unit_cost, i.costo_unitario, 0)
            ) AS base_cost
        FROM product_base_recipes pbr
        JOIN base_recipe_templates brt ON pbr.product_base_type_id = brt.product_base_type_id
        JOIN ingredients i ON brt.ingredient_id = i.id
        LEFT JOIN latest_purchase_costs lpc ON brt.ingredient_id = lpc.ingredient_id
        GROUP BY pbr.product_id
    )
"""

_CALCULATE_PRODUCT_COST_SQL = """
    WITH latest_purchase_costs AS (
        SELECT DISTINCT ON (pi.ingredient_id)
            pi.ingredient_id,
            pi.unit_cost
        FROM tenant_purchase_items pi
        JOIN tenant_purchases tp ON pi.purchase_id = tp.id
        WHERE tp.tenant_id = $2
          AND pi.unit_cost IS NOT NULL
          AND pi.unit_cost > 0
        ORDER BY pi.ingredient_id, tp.purchase_date DESC
    ),
    direct_cost AS (
        SELECT COALESCE(SUM(
            pr.quantity * COALESCE(lpc.unit_cost, i.costo_unitario, 0)
        ), 0) AS amount
        FROM product_recipes pr
        JOIN ingredients i ON pr.ingredient_id = i.id
        LEFT JOIN latest_purchase_costs lpc ON pr.ingredient_id = lpc.ingredient_id
        WHERE pr.product_id = $1
    ),
    base_cost AS (
        SELECT COALESCE(SUM(
            pbr.quantity * brt.base_quantity * COALESCE(lpc.unit_cost, i.costo_unitario, 0)
        ), 0) AS amount
        FROM product_base_recipes pbr
        JOIN base_recipe_templates brt ON pbr.product_base_type_id = brt.product_base_type_id
        JOIN ingredients i ON brt.ingredient_id = i.id
        LEFT JOIN latest_purchase_costs lpc ON brt.ingredient_id = lpc.ingredient_id
        WHERE pbr.product_id = $1
    )
    SELECT (SELECT amount FROM direct_cost) + (SELECT amount FROM base_cost) AS total_cost
"""


async def calculated_product_cost_real(
    product_id: UUID,
    tenant_id: UUID,
    conn,
) -> Decimal:
    """Return real calculated cost for a product (direct recipes + base recipes)."""
    row = await conn.fetchrow(
        _CALCULATE_PRODUCT_COST_SQL,
        product_id,
        tenant_id,
    )
    if not row or row["total_cost"] is None:
        return Decimal("0")
    return Decimal(str(row["total_cost"]))


async def product_has_any_recipe(product_id: UUID, conn) -> bool:
    return await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM product_recipes WHERE product_id = $1
            UNION ALL
            SELECT 1 FROM product_base_recipes WHERE product_id = $1
        )
        """,
        product_id,
    )


async def ingredient_has_purchase_unit_cost(
    conn,
    *,
    tenant_id: UUID,
    ingredient_id: UUID,
) -> bool:
    """True when a purchase line supplies a positive unit_cost for this ingredient."""
    return bool(
        await conn.fetchval(
            """
            SELECT EXISTS(
                SELECT 1
                FROM tenant_purchase_items pi
                JOIN tenant_purchases tp ON pi.purchase_id = tp.id
                WHERE tp.tenant_id = $1
                  AND pi.ingredient_id = $2
                  AND pi.unit_cost IS NOT NULL
                  AND pi.unit_cost > 0
            )
            """,
            tenant_id,
            ingredient_id,
        )
    )


async def sync_resale_mi_costo_to_ingredient(
    conn,
    *,
    tenant_id: UUID,
    product_id: UUID,
    costo_percibido: Optional[Decimal],
) -> bool:
    """
    For resale products, seed linked warehouse ingredient.costo_unitario from Mi costo
    when no purchase-based unit cost exists. Returns True if the ingredient was updated.
    """
    ingredient_id = await conn.fetchval(
        """
        SELECT i.id
        FROM product p
        JOIN product_recipes pr ON pr.product_id = p.id
        JOIN ingredients i ON i.id = pr.ingredient_id
        WHERE p.id = $1
          AND p.tenant_id = $2
          AND p.is_resale = true
          AND i.tenant_id = $2
          AND i.is_resale = true
        ORDER BY pr.created_at ASC
        LIMIT 1
        """,
        product_id,
        tenant_id,
    )
    if ingredient_id is None:
        return False

    # Match create seeding: only write a concrete Mi costo, never wipe via null.
    if costo_percibido is None:
        return False

    if await ingredient_has_purchase_unit_cost(
        conn,
        tenant_id=tenant_id,
        ingredient_id=ingredient_id,
    ):
        return False

    await conn.execute(
        """
        UPDATE ingredients
        SET costo_unitario = $2,
            updated_at = NOW()
        WHERE id = $1
          AND tenant_id = $3
        """,
        ingredient_id,
        float(costo_percibido),
        tenant_id,
    )
    return True


async def persist_product_costo_calculado(
    product_id: UUID,
    tenant_id: UUID,
    conn,
    *,
    tracks_inventory: bool,
) -> None:
    """
    Write product.costo_calculado using the unified resolver.
    NULL when the product does not track inventory / has no recipe.
    """
    if not tracks_inventory:
        await conn.execute(
            "UPDATE product SET costo_calculado = NULL WHERE id = $1",
            product_id,
        )
        return

    has_recipe = await product_has_any_recipe(product_id, conn)
    if not has_recipe:
        await conn.execute(
            "UPDATE product SET costo_calculado = NULL WHERE id = $1",
            product_id,
        )
        return

    total = await calculated_product_cost_real(product_id, tenant_id, conn)
    await conn.execute(
        "UPDATE product SET costo_calculado = $2 WHERE id = $1",
        product_id,
        total,
    )


async def recalculate_products_for_ingredient(
    ingredient_id: UUID,
    tenant_id: UUID,
    conn,
) -> None:
    """Recalculate costo_calculado for all products that use an ingredient."""
    product_ids = await conn.fetch(
        """
        SELECT DISTINCT product_id FROM (
            SELECT pr.product_id
            FROM product_recipes pr
            WHERE pr.ingredient_id = $1
            UNION
            SELECT pbr.product_id
            FROM product_base_recipes pbr
            JOIN base_recipe_templates brt ON pbr.product_base_type_id = brt.product_base_type_id
            WHERE brt.ingredient_id = $1
        ) affected
        """,
        ingredient_id,
    )
    for row in product_ids:
        pid = row["product_id"]
        await persist_product_costo_calculado(
            pid,
            tenant_id,
            conn,
            tracks_inventory=True,
        )


def apply_analytics_cost_fallback(calc_cost: Decimal, price: Decimal) -> Decimal:
    """
    Analytics-only: when real cost is missing or exceeds price, use 40% of price.
    Menu product list/detail do not apply this rule.
    """
    if calc_cost <= 0 or calc_cost > price:
        return price * Decimal("0.40")
    return calc_cost

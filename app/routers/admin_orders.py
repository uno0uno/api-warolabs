"""
Admin Orders Router — backfill and maintenance endpoints (issue #105)
"""
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Query, Request

from app.core.middleware import require_valid_session
from app.database import get_db_connection
from app.services.pos_cart_service import _get_last_purchase_prices

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/orders", tags=["Orders Admin"])


@router.post("/backfill-order-ingredients", status_code=200)
async def backfill_order_ingredients(
    request: Request,
    batch_size: int = Query(default=500, ge=1, le=2000),
    dry_run: bool = Query(default=False, description="When true, count rows without inserting"),
) -> Dict[str, Any]:
    """
    Backfill order_item_ingredients for historical order_items that have no ingredient snapshots.

    Sources:
    - product_recipes (source_type = 'product_recipe')
    - product_base_recipes → base_recipe_templates (source_type = 'base_recipe')
    - order_item_modifiers → modifiers.ingredient_id (source_type = 'modifier')

    Idempotent: ON CONFLICT (order_item_id, ingredient_id) DO NOTHING.
    Safe to run multiple times.

    dry_run=true returns counts without inserting anything.
    """
    require_valid_session(request)

    async with get_db_connection() as conn:
        # ── Dry run counts ────────────────────────────────────────────────────
        count_product_recipes = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM order_items oi
            LEFT JOIN order_item_ingredients oii ON oii.order_item_id = oi.id
            JOIN product_recipes pr ON pr.product_id = oi.product_id
            WHERE oii.id IS NULL AND oi.product_id IS NOT NULL
            """
        )
        count_base_recipes = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM order_items oi
            LEFT JOIN order_item_ingredients oii ON oii.order_item_id = oi.id
            JOIN product_base_recipes pbr ON pbr.product_id = oi.product_id
            JOIN base_recipe_templates brt ON brt.product_base_type_id = pbr.product_base_type_id
            WHERE oii.id IS NULL AND oi.product_id IS NOT NULL
            """
        )
        count_modifiers = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM order_item_modifiers oim
            JOIN modifiers m ON m.id = oim.modifier_id AND m.ingredient_id IS NOT NULL
            LEFT JOIN order_item_ingredients oii
                ON oii.order_item_id = oim.order_item_id AND oii.ingredient_id = m.ingredient_id
            WHERE oii.id IS NULL
            """
        )
        total_to_insert = int(count_product_recipes) + int(count_base_recipes) + int(count_modifiers)

        if dry_run:
            return {
                "success": True,
                "dry_run": True,
                "to_insert": {
                    "via_product_recipes": int(count_product_recipes),
                    "via_base_recipes": int(count_base_recipes),
                    "via_modifiers": int(count_modifiers),
                    "total": total_to_insert,
                },
            }

        # ── Actual backfill ───────────────────────────────────────────────────
        inserted = 0
        skipped = 0

        # 1. product_recipes + base_recipe_templates — grouped by order_item
        order_item_ids: List[Any] = await conn.fetch(
            """
            SELECT DISTINCT oi.id, oi.product_id, oi.quantity,
                            o.tenant_id
            FROM order_items oi
            JOIN orders o ON o.id = oi.order_id
            LEFT JOIN order_item_ingredients oii ON oii.order_item_id = oi.id
            WHERE oii.id IS NULL AND oi.product_id IS NOT NULL
              AND (
                EXISTS (SELECT 1 FROM product_recipes pr WHERE pr.product_id = oi.product_id)
                OR EXISTS (
                    SELECT 1 FROM product_base_recipes pbr
                    JOIN base_recipe_templates brt ON brt.product_base_type_id = pbr.product_base_type_id
                    WHERE pbr.product_id = oi.product_id
                )
              )
            ORDER BY oi.id
            LIMIT $1
            """,
            batch_size,
        )

        for row in order_item_ids:
            order_item_id = row["id"]
            product_id = row["product_id"]
            item_quantity = float(row["quantity"])
            tenant_id = str(row["tenant_id"])

            ingredients = await conn.fetch(
                """
                SELECT
                    pr.id::text       AS source_id,
                    'PRODUCT_RECIPE'  AS source_type,
                    pr.ingredient_id,
                    i.name            AS ingredient_name,
                    pr.quantity,
                    pr.unit
                FROM product_recipes pr
                JOIN ingredients i ON i.id = pr.ingredient_id
                WHERE pr.product_id = $1

                UNION ALL

                SELECT
                    brt.id::text      AS source_id,
                    'PRODUCT_RECIPE'  AS source_type,
                    brt.ingredient_id,
                    i.name            AS ingredient_name,
                    brt.base_quantity AS quantity,
                    brt.unit
                FROM product_base_recipes pbr
                JOIN base_recipe_templates brt ON brt.product_base_type_id = pbr.product_base_type_id
                JOIN ingredients i ON i.id = brt.ingredient_id
                WHERE pbr.product_id = $1
                """,
                product_id,
            )

            if not ingredients:
                continue

            ingredient_ids = [str(r["ingredient_id"]) for r in ingredients]
            prices = await _get_last_purchase_prices(conn, ingredient_ids, tenant_id)

            for r in ingredients:
                ingredient_id_str = str(r["ingredient_id"])
                quantity = float(r["quantity"]) * item_quantity
                unit_cost = prices.get(ingredient_id_str)
                total_cost = quantity * unit_cost if unit_cost is not None else None

                result = await conn.fetchval(
                    """
                    INSERT INTO order_item_ingredients (
                        order_item_id, ingredient_id, ingredient_name,
                        quantity, unit, unit_cost, total_cost,
                        source_type, source_id, created_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::uuid, NOW())
                    ON CONFLICT (order_item_id, ingredient_id) DO NOTHING
                    RETURNING id
                    """,
                    order_item_id,
                    r["ingredient_id"],
                    r["ingredient_name"],
                    quantity,
                    r["unit"] or "und",
                    unit_cost,
                    total_cost,
                    r["source_type"],
                    r["source_id"],
                )
                if result:
                    inserted += 1
                else:
                    skipped += 1

        # 2. Modifier ingredients
        modifier_rows: List[Any] = await conn.fetch(
            """
            SELECT
                oim.order_item_id,
                oim.modifier_id,
                oim.quantity       AS modifier_qty,
                oi.quantity        AS item_qty,
                m.ingredient_id,
                i.name             AS ingredient_name,
                m.ingredient_quantity,
                m.ingredient_unit,
                o.tenant_id
            FROM order_item_modifiers oim
            JOIN modifiers m ON m.id = oim.modifier_id
                AND m.ingredient_id IS NOT NULL
                AND m.ingredient_quantity IS NOT NULL
                AND m.ingredient_quantity > 0
            JOIN order_items oi ON oi.id = oim.order_item_id
            JOIN orders o ON o.id = oi.order_id
            JOIN ingredients i ON i.id = m.ingredient_id
            LEFT JOIN order_item_ingredients oii
                ON oii.order_item_id = oim.order_item_id AND oii.ingredient_id = m.ingredient_id
            WHERE oii.id IS NULL
            LIMIT $1
            """,
            batch_size,
        )

        for row in modifier_rows:
            tenant_id = str(row["tenant_id"])
            ingredient_id_str = str(row["ingredient_id"])
            prices = await _get_last_purchase_prices(conn, [ingredient_id_str], tenant_id)

            quantity = (
                float(row["ingredient_quantity"]) *
                float(row["item_qty"]) *
                float(row["modifier_qty"] or 1)
            )
            unit_cost = prices.get(ingredient_id_str)
            total_cost = quantity * unit_cost if unit_cost is not None else None

            result = await conn.fetchval(
                """
                INSERT INTO order_item_ingredients (
                    order_item_id, ingredient_id, ingredient_name,
                    quantity, unit, unit_cost, total_cost,
                    source_type, source_id, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::uuid, NOW())
                ON CONFLICT (order_item_id, ingredient_id) DO NOTHING
                RETURNING id
                """,
                row["order_item_id"],
                row["ingredient_id"],
                row["ingredient_name"],
                quantity,
                row["ingredient_unit"] or "und",
                unit_cost,
                total_cost,
                "MODIFIER_RECIPE",
                str(row["modifier_id"]),
            )
            if result:
                inserted += 1
            else:
                skipped += 1

    remaining = total_to_insert - inserted
    return {
        "success": True,
        "dry_run": False,
        "batch_size": batch_size,
        "inserted": inserted,
        "skipped_duplicates": skipped,
        "remaining_estimate": max(0, remaining),
        "complete": remaining <= 0,
    }

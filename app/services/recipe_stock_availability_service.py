"""Recipe-stock availability for catalog visibility (warocol.com#2574 / #2579).

When `tenant_public_profiles.hide_products_without_stock` is on, selling
catalogs soft-hide products that cannot make qty>=1 from current
`tenant_inventory` via direct bodega lines, recipe bases, or linked resale
warehouse articles. Non-resale products with no composition stay visible.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import UUID

import asyncpg

from app.services.ingredient_purchase_units_service import _CATALOG_TO_BASE

logger = logging.getLogger(__name__)

_QUANTITY_SCALE = Decimal("0.000001")


def _decimal_value(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    if value is None:
        return default
    return Decimal(str(value))


def _quantity_decimal(value: Any) -> Decimal:
    return _decimal_value(value).quantize(_QUANTITY_SCALE)


def resolve_recipe_qty_with_meta(
    recipe_qty: Any,
    recipe_unit: str,
    base_unit: Optional[str],
    unit_weight_gr: Any,
) -> float:
    """Same conversions as `resolve_recipe_quantity_to_base_unit`, without DB I/O."""
    recipe_quantity = _quantity_decimal(recipe_qty)
    if not base_unit:
        return float(recipe_quantity)
    if recipe_unit == base_unit:
        return float(recipe_quantity)
    weight = unit_weight_gr
    if weight and _decimal_value(weight) > 0:
        w = _decimal_value(weight)
        if recipe_unit in ("gr", "ml") and base_unit == "und":
            return float(_quantity_decimal(recipe_quantity / w))
        if recipe_unit == "und" and base_unit in ("gr", "ml"):
            return float(_quantity_decimal(recipe_quantity * w))
        catalog_entry = _CATALOG_TO_BASE.get(recipe_unit)
        if catalog_entry and base_unit == "und" and catalog_entry["base"] in ("gr", "ml"):
            qty_base = recipe_quantity * Decimal(str(catalog_entry["factor"]))
            return float(_quantity_decimal(qty_base / w))
    return float(recipe_quantity)


async def is_hide_products_without_stock_enabled(conn, tenant_id: UUID) -> bool:
    """True only when the column exists and is explicitly true."""
    try:
        value = await conn.fetchval(
            """
            SELECT hide_products_without_stock
            FROM tenant_public_profiles
            WHERE tenant_id = $1
            """,
            tenant_id,
        )
    except asyncpg.UndefinedColumnError:
        logger.warning(
            "hide_products_without_stock missing; treating as off (warocol.com#2574)"
        )
        return False
    return value is True


async def product_ids_insufficient_recipe_stock(
    conn,
    tenant_id: UUID,
) -> Set[UUID]:
    """Product IDs that cannot satisfy qty>=1 from warehouse inventory.

    Composition sources (warocol.com#2579):
    - Direct bodega lines (`product_recipes`)
    - Recipe bases (`product_base_recipes` → templates)
    - Resale without recipe rows: name-match to `ingredients.is_resale`
    - Resale with no resolvable warehouse article → treated as insufficient

    Missing inventory rows count as 0 stock. Non-resale products with no
    expandable composition are never returned (they stay visible).
    """
    rows = await conn.fetch(
        """
        SELECT
            r.product_id,
            r.ingredient_id,
            r.quantity,
            COALESCE(r.unit, '') AS unit
        FROM (
            SELECT
                pr.product_id,
                pr.ingredient_id,
                pr.quantity,
                pr.unit
            FROM product_recipes pr
            JOIN product p ON p.id = pr.product_id
            WHERE p.tenant_id = $1

            UNION ALL

            SELECT
                pbr.product_id,
                brt.ingredient_id,
                brt.base_quantity * pbr.quantity AS quantity,
                brt.unit
            FROM product_base_recipes pbr
            JOIN product p ON p.id = pbr.product_id
            JOIN base_recipe_templates brt
                ON pbr.product_base_type_id = brt.product_base_type_id
            WHERE p.tenant_id = $1

            UNION ALL

            -- Orphan resale: no recipe rows — link by name to warehouse article
            SELECT
                p.id AS product_id,
                i.id AS ingredient_id,
                1::numeric AS quantity,
                'und'::text AS unit
            FROM product p
            JOIN ingredients i
                ON i.tenant_id = p.tenant_id
               AND i.is_resale = true
               AND lower(trim(i.name)) = lower(trim(p.name))
            WHERE p.tenant_id = $1
              AND p.is_resale = true
              AND NOT EXISTS (
                  SELECT 1 FROM product_recipes pr WHERE pr.product_id = p.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM product_base_recipes pbr WHERE pbr.product_id = p.id
              )
        ) r
        """,
        tenant_id,
    )

    insufficient: Set[UUID] = set()

    # Resale with neither composition nor name-matched article → hide when flag on
    unlinked_resale = await conn.fetch(
        """
        SELECT p.id
        FROM product p
        WHERE p.tenant_id = $1
          AND p.is_resale = true
          AND NOT EXISTS (
              SELECT 1 FROM product_recipes pr WHERE pr.product_id = p.id
          )
          AND NOT EXISTS (
              SELECT 1 FROM product_base_recipes pbr WHERE pbr.product_id = p.id
          )
          AND NOT EXISTS (
              SELECT 1
              FROM ingredients i
              WHERE i.tenant_id = p.tenant_id
                AND i.is_resale = true
                AND lower(trim(i.name)) = lower(trim(p.name))
          )
        """,
        tenant_id,
    )
    for row in unlinked_resale:
        insufficient.add(row["id"])

    if not rows:
        return insufficient

    ingredient_ids = list({row["ingredient_id"] for row in rows})
    ing_rows = await conn.fetch(
        """
        SELECT id, unit, unit_weight_gr
        FROM ingredients
        WHERE id = ANY($1::uuid[])
        """,
        ingredient_ids,
    )
    ing_meta: Dict[UUID, Any] = {row["id"]: row for row in ing_rows}

    stock_rows = await conn.fetch(
        """
        SELECT ingredient_id, current_stock
        FROM tenant_inventory
        WHERE tenant_id = $1
          AND ingredient_id = ANY($2::uuid[])
        """,
        tenant_id,
        ingredient_ids,
    )
    stock: Dict[UUID, float] = {
        row["ingredient_id"]: float(row["current_stock"] or 0)
        for row in stock_rows
    }

    required: Dict[UUID, Dict[UUID, float]] = defaultdict(lambda: defaultdict(float))
    for row in rows:
        meta = ing_meta.get(row["ingredient_id"])
        base_unit = meta["unit"] if meta else None
        weight = meta["unit_weight_gr"] if meta else None
        need = resolve_recipe_qty_with_meta(
            row["quantity"],
            row["unit"] or "",
            base_unit,
            weight,
        )
        required[row["product_id"]][row["ingredient_id"]] += need

    for product_id, ingredients in required.items():
        for ingredient_id, need in ingredients.items():
            if stock.get(ingredient_id, 0.0) < need:
                insufficient.add(product_id)
                break
    return insufficient


async def apply_hide_products_without_stock_filter(
    conn,
    tenant_id: UUID,
    products_query: str,
    params: List[Any],
) -> Tuple[str, List[Any]]:
    """Append SQL excluding insufficient-recipe-stock products when flag is on.

    Used by online menu (#2575) and table QR (#2576) listings.
    """
    if not await is_hide_products_without_stock_enabled(conn, tenant_id):
        return products_query, params
    hide_ids = await product_ids_insufficient_recipe_stock(conn, tenant_id)
    if not hide_ids:
        return products_query, params
    next_param = len(params) + 1
    products_query += f" AND p.id <> ALL(${next_param}::uuid[])"
    params = list(params) + [list(hide_ids)]
    return products_query, params

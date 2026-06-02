"""
Resolve modifier option composition (ingredient / recipe / product) for cost and inventory.

Issue warocol.com#1121.
"""
from decimal import Decimal
from typing import Any, Dict, List, Optional
from uuid import UUID

from app.services.cost_resolution_service import calculated_product_cost_real
from app.services.ingredient_purchase_units_service import resolve_recipe_quantity_to_base_unit


OPTION_TYPES = frozenset({"INGREDIENT", "RECIPE", "PRODUCT", "NONE"})


async def fetch_modifier_option_row(conn, modifier_id: UUID) -> Optional[Dict[str, Any]]:
    return await conn.fetchrow(
        """
        SELECT
            m.id,
            m.option_type,
            m.ingredient_id,
            m.ingredient_quantity,
            m.ingredient_unit,
            m.recipe_base_type_id,
            m.recipe_base_quantity,
            m.linked_product_id,
            m.linked_product_quantity,
            i.name AS ingredient_name,
            i.controla_inventario
        FROM modifiers m
        LEFT JOIN ingredients i ON m.ingredient_id = i.id
        WHERE m.id = $1
        """,
        modifier_id,
    )


async def resolve_modifier_ingredient_lines(
    conn,
    modifier_id: UUID,
    tenant_id: UUID,
) -> List[Dict[str, Any]]:
    """
    Ingredient consumption per one modifier selection (before order line qty multipliers).
    Each row: ingredient_id, quantity, unit, ingredient_name, controla_inventario.
    """
    row = await fetch_modifier_option_row(conn, modifier_id)
    if not row:
        return []

    option_type = (row["option_type"] or "INGREDIENT").upper()

    if option_type == "NONE":
        return []

    if option_type == "INGREDIENT":
        if not row["ingredient_id"] or not row["ingredient_quantity"]:
            return []
        resolved_qty = await resolve_recipe_quantity_to_base_unit(
            conn,
            row["ingredient_id"],
            float(row["ingredient_quantity"]),
            row["ingredient_unit"] or "",
        )
        return [
            {
                "ingredient_id": row["ingredient_id"],
                "quantity": resolved_qty,
                "unit": row["ingredient_unit"] or "und",
                "ingredient_name": row["ingredient_name"],
                "controla_inventario": row["controla_inventario"],
            }
        ]

    if option_type == "RECIPE":
        lines: List[Dict[str, Any]] = []
        base_qty = float(row["recipe_base_quantity"] or 1)

        if row["recipe_base_type_id"]:
            base_rows = await conn.fetch(
                """
                SELECT
                    brt.ingredient_id,
                    brt.base_quantity * $2 AS quantity,
                    brt.unit,
                    i.name AS ingredient_name,
                    i.controla_inventario
                FROM base_recipe_templates brt
                JOIN ingredients i ON brt.ingredient_id = i.id
                WHERE brt.product_base_type_id = $1
                """,
                row["recipe_base_type_id"],
                base_qty,
            )
            for br in base_rows:
                resolved = await resolve_recipe_quantity_to_base_unit(
                    conn,
                    br["ingredient_id"],
                    float(br["quantity"]),
                    br["unit"] or "",
                )
                lines.append(
                    {
                        "ingredient_id": br["ingredient_id"],
                        "quantity": resolved,
                        "unit": br["unit"] or "und",
                        "ingredient_name": br["ingredient_name"],
                        "controla_inventario": br["controla_inventario"],
                    }
                )

        recipe_rows = await conn.fetch(
            """
            SELECT
                mr.ingredient_id,
                mr.quantity,
                mr.unit,
                i.name AS ingredient_name,
                i.controla_inventario
            FROM modifier_recipes mr
            JOIN ingredients i ON mr.ingredient_id = i.id
            WHERE mr.modifier_id = $1
            """,
            modifier_id,
        )
        for rr in recipe_rows:
            resolved = await resolve_recipe_quantity_to_base_unit(
                conn,
                rr["ingredient_id"],
                float(rr["quantity"]),
                rr["unit"] or "",
            )
            lines.append(
                {
                    "ingredient_id": rr["ingredient_id"],
                    "quantity": resolved,
                    "unit": rr["unit"] or "und",
                    "ingredient_name": rr["ingredient_name"],
                    "controla_inventario": rr["controla_inventario"],
                }
            )
        return _merge_ingredient_lines(lines)

    if option_type == "PRODUCT":
        product_id = row["linked_product_id"]
        if not product_id:
            return []
        mult = float(row["linked_product_quantity"] or 1)
        product_rows = await conn.fetch(
            """
            SELECT
                pr.ingredient_id,
                pr.quantity * $2 AS quantity,
                pr.unit,
                i.name AS ingredient_name,
                i.controla_inventario
            FROM product_recipes pr
            JOIN ingredients i ON pr.ingredient_id = i.id
            WHERE pr.product_id = $1

            UNION ALL

            SELECT
                brt.ingredient_id,
                brt.base_quantity * pbr.quantity * $2 AS quantity,
                brt.unit,
                i.name AS ingredient_name,
                i.controla_inventario
            FROM product_base_recipes pbr
            JOIN base_recipe_templates brt ON pbr.product_base_type_id = brt.product_base_type_id
            JOIN ingredients i ON brt.ingredient_id = i.id
            WHERE pbr.product_id = $1
            """,
            product_id,
            mult,
        )
        lines = []
        for pr in product_rows:
            resolved = await resolve_recipe_quantity_to_base_unit(
                conn,
                pr["ingredient_id"],
                float(pr["quantity"]),
                pr["unit"] or "",
            )
            lines.append(
                {
                    "ingredient_id": pr["ingredient_id"],
                    "quantity": resolved,
                    "unit": pr["unit"] or "und",
                    "ingredient_name": pr["ingredient_name"],
                    "controla_inventario": pr["controla_inventario"],
                }
            )
        return _merge_ingredient_lines(lines)

    return []


def _merge_ingredient_lines(lines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sum quantities when the same ingredient appears from multiple recipe sources."""
    merged: Dict[Any, Dict[str, Any]] = {}
    for line in lines:
        ing_id = line["ingredient_id"]
        if ing_id in merged:
            merged[ing_id]["quantity"] = float(merged[ing_id]["quantity"]) + float(line["quantity"])
        else:
            merged[ing_id] = dict(line)
    return list(merged.values())


async def calculated_modifier_option_unit_cost(
    conn,
    modifier_id: UUID,
    tenant_id: UUID,
) -> Decimal:
    """Unit cost for one modifier selection (menu preview / API embedded field)."""
    lines = await resolve_modifier_ingredient_lines(conn, modifier_id, tenant_id)
    if not lines:
        row = await fetch_modifier_option_row(conn, modifier_id)
        if row and (row["option_type"] or "").upper() == "PRODUCT" and row["linked_product_id"]:
            total = await calculated_product_cost_real(
                row["linked_product_id"],
                tenant_id,
                conn,
            )
            mult = Decimal(str(row["linked_product_quantity"] or 1))
            return total * mult
        return Decimal("0")

    ingredient_ids = [str(line["ingredient_id"]) for line in lines]
    prices = await _get_ingredient_unit_costs(conn, ingredient_ids, tenant_id)
    total = Decimal("0")
    for line in lines:
        unit_cost = prices.get(str(line["ingredient_id"]), Decimal("0"))
        total += Decimal(str(line["quantity"])) * unit_cost
    return total


async def _get_ingredient_unit_costs(
    conn,
    ingredient_ids: List[str],
    tenant_id: UUID,
) -> Dict[str, Decimal]:
    if not ingredient_ids:
        return {}
    rows = await conn.fetch(
        """
        WITH latest_purchase_costs AS (
            SELECT DISTINCT ON (pi.ingredient_id)
                pi.ingredient_id,
                pi.unit_cost
            FROM tenant_purchase_items pi
            JOIN tenant_purchases tp ON pi.purchase_id = tp.id
            WHERE tp.tenant_id = $1
              AND pi.ingredient_id = ANY($2::uuid[])
              AND pi.unit_cost IS NOT NULL
              AND pi.unit_cost > 0
            ORDER BY pi.ingredient_id, tp.purchase_date DESC
        )
        SELECT i.id AS ingredient_id,
               COALESCE(lpc.unit_cost, i.costo_unitario, 0) AS unit_cost
        FROM ingredients i
        LEFT JOIN latest_purchase_costs lpc ON i.id = lpc.ingredient_id
        WHERE i.id = ANY($2::uuid[])
        """,
        tenant_id,
        ingredient_ids,
    )
    return {str(r["ingredient_id"]): Decimal(str(r["unit_cost"])) for r in rows}


def validate_modifier_option_fields(modifier_data) -> None:
    """Raise ValueError when option_type and FKs are inconsistent."""
    option_type = (getattr(modifier_data, "option_type", None) or "INGREDIENT").upper()
    if option_type not in OPTION_TYPES:
        raise ValueError(f"Invalid option_type: {option_type}")

    ingredient_id = getattr(modifier_data, "ingredient_id", None)
    recipe_base_type_id = getattr(modifier_data, "recipe_base_type_id", None)
    recipe_lines = getattr(modifier_data, "recipe_lines", None) or []
    linked_product_id = getattr(modifier_data, "linked_product_id", None)

    if option_type == "INGREDIENT" and not ingredient_id:
        raise ValueError("INGREDIENT option requires ingredient_id")
    if option_type == "RECIPE" and not recipe_base_type_id and not recipe_lines:
        raise ValueError("RECIPE option requires recipe_base_type_id or recipe_lines")
    if option_type == "PRODUCT" and not linked_product_id:
        raise ValueError("PRODUCT option requires linked_product_id")
    if option_type == "NONE" and any(
        [ingredient_id, recipe_base_type_id, linked_product_id, recipe_lines]
    ):
        raise ValueError("NONE option must not set composition FKs")

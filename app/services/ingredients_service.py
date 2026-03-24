from typing import List, Optional
from uuid import UUID
from fastapi import Request, Response, HTTPException
from app.database import get_db_connection
from app.core.middleware import require_valid_session
import logging
from app.core.exceptions import AuthenticationError
from app.models.ingredient import Ingredient, IngredientsListResponse

logger = logging.getLogger(__name__)

async def get_ingredients_list(
    request: Request,
    response: Response,
    page: int = 1,
    limit: int = 50,
    search: Optional[str] = None,
    category: Optional[str] = None,
    supplier_id: Optional[UUID] = None,
    type: Optional[str] = None,
    is_resale: Optional[bool] = None,
    base_only: Optional[bool] = None,
) -> IngredientsListResponse:
    """
    Fetches a list of ingredients from the database with tenant isolation,
    joining with tenant_supplier_prices to get the current price.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Base query joins ingredients (global catalog) with tenant-specific prices
            # Falls back to unit_cost from ingredient_purchase_units when no manual price exists
            base_query = """
                SELECT
                    i.id,
                    i.tenant_id,
                    i.name,
                    i.unit,
                    i.category,
                    i.type,
                    i.description,
                    CAST(i.minimum_order_quantity AS float) as minimum_order_quantity,
                    CAST(i.unit_weight_gr AS float) as unit_weight_gr,
                    i.created_at,
                    i.updated_at,
                    CAST(COALESCE(tsp.unit_price, tim.cost_per_unit) AS float) as price,
                    tsp.supplier_id,
                    igh.base_id::text   AS hierarchy_base_id,
                    hb.name             AS hierarchy_base_name,
                    COALESCE((
                        SELECT COUNT(*)::int FROM ingredient_global_hierarchy
                        WHERE base_id = i.id
                    ), 0) AS has_variants
                FROM ingredients i
                LEFT JOIN (
                    SELECT
                        ingredient_id,
                        supplier_id,
                        unit_price,
                        ROW_NUMBER() OVER(PARTITION BY ingredient_id ORDER BY effective_date DESC, created_at DESC) as rn
                    FROM tenant_supplier_prices
                    WHERE tenant_id = $1 AND is_active = TRUE
                ) tsp ON i.id = tsp.ingredient_id AND tsp.rn = 1
                LEFT JOIN (
                    SELECT
                        ingredient_id,
                        cost_per_unit,
                        ROW_NUMBER() OVER(PARTITION BY ingredient_id ORDER BY created_at DESC) as rn
                    FROM tenant_ingredient_movements
                    WHERE tenant_id = $1 AND movement_type = 'purchase'
                      AND cost_per_unit IS NOT NULL AND cost_per_unit > 0
                ) tim ON i.id = tim.ingredient_id AND tim.rn = 1
                LEFT JOIN ingredient_global_hierarchy igh ON igh.variant_id = i.id
                LEFT JOIN ingredients hb ON hb.id = igh.base_id
                WHERE 1=1
            """

            count_query = "SELECT COUNT(*) FROM ingredients WHERE 1=1"

            # Separate params for base query (includes tenant_id) and count query (no tenant_id)
            base_params = [tenant_id]
            count_params = []
            base_param_count = 2
            count_param_count = 1

            # Add filters
            if search:
                base_query += f" AND (LOWER(i.name) LIKE LOWER(${base_param_count}) OR LOWER(i.description) LIKE LOWER(${base_param_count}))"
                count_query += f" AND (LOWER(name) LIKE LOWER(${count_param_count}) OR LOWER(description) LIKE LOWER(${count_param_count}))"
                base_params.append(f"%{search}%")
                count_params.append(f"%{search}%")
                base_param_count += 1
                count_param_count += 1

            if category:
                base_query += f" AND LOWER(i.category) = LOWER(${base_param_count})"
                count_query += f" AND LOWER(category) = LOWER(${count_param_count})"
                base_params.append(category)
                count_params.append(category)
                base_param_count += 1
                count_param_count += 1

            if type:
                base_query += f" AND LOWER(i.type) = LOWER(${base_param_count})"
                count_query += f" AND LOWER(type) = LOWER(${count_param_count})"
                base_params.append(type)
                count_params.append(type)
                base_param_count += 1
                count_param_count += 1

            if supplier_id:
                base_query += f" AND tsp.supplier_id = ${base_param_count}"
                # Note: Filtering count by supplier_id would require a join in the count query as well.
                # For simplicity, we'll count all ingredients and filter the result set.
                base_params.append(supplier_id)
                base_param_count += 1

            if is_resale is not None:
                base_query += f" AND i.is_resale = ${base_param_count}"
                count_query += f" AND is_resale = ${count_param_count}"
                base_params.append(is_resale)
                count_params.append(is_resale)
                base_param_count += 1
                count_param_count += 1

            if base_only:
                # Exclude ingredients that are variants (have a base assigned)
                base_query += " AND NOT EXISTS (SELECT 1 FROM ingredient_global_hierarchy WHERE variant_id = i.id)"
                count_query += " AND NOT EXISTS (SELECT 1 FROM ingredient_global_hierarchy WHERE variant_id = id)"

            # Add pagination
            offset = (page - 1) * limit
            base_query += f" ORDER BY i.created_at DESC LIMIT ${base_param_count} OFFSET ${base_param_count + 1}"
            base_params.extend([limit, offset])

            # Execute queries
            ingredients_data = await conn.fetch(base_query, *base_params)
            count_result = await conn.fetchrow(count_query, *count_params)

            # Process results into Pydantic models
            ingredients = []
            from pydantic import ValidationError
            for row in ingredients_data:
                try:
                    ingredients.append(Ingredient(**row))
                except ValidationError as e:
                    # Continue to the next row instead of raising
                    continue

            return IngredientsListResponse(
                success=True,
                total=count_result['count'],
                data=ingredients
            )

    except AuthenticationError:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal Server Error")


async def update_ingredient_unit_weight(
    request: Request,
    response: Response,
    ingredient_id: UUID,
    unit_weight_gr: Optional[float]
) -> dict:
    """
    Updates the unit_weight_gr field on an ingredient.
    Idempotent — safe to call multiple times with the same value.
    """
    try:
        require_valid_session(request)

        async with get_db_connection() as conn:
            row = await conn.fetchrow(
                "SELECT id, unit FROM ingredients WHERE id = $1",
                ingredient_id
            )
            if not row:
                raise HTTPException(status_code=404, detail="Ingredient not found")

            await conn.execute(
                "UPDATE ingredients SET unit_weight_gr = $1, updated_at = NOW() WHERE id = $2",
                unit_weight_gr,
                ingredient_id
            )

            logger.info(f"Updated unit_weight_gr={unit_weight_gr} for ingredient {ingredient_id}")

            return {
                "success": True,
                "ingredient_id": str(ingredient_id),
                "unit_weight_gr": unit_weight_gr
            }

    except HTTPException:
        raise
    except AuthenticationError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        logger.error(f"Error updating unit_weight_gr: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal Server Error")


async def match_ingredient_by_name(conn, name: str, threshold: float = 0.35) -> Optional[dict]:
    """Find closest ingredient by name using pg_trgm similarity."""
    row = await conn.fetchrow("""
        SELECT id, name, unit, type, unit_weight_gr, similarity(name, $1) as score
        FROM ingredients
        WHERE similarity(name, $1) > $2
        ORDER BY similarity(name, $1) DESC
        LIMIT 1
    """, name, threshold)
    if row:
        return {
            "id": str(row["id"]),
            "name": row["name"],
            "unit": row["unit"],
            "type": row["type"],
            "unit_weight_gr": float(row["unit_weight_gr"]) if row["unit_weight_gr"] is not None else None,
            "score": float(row["score"])
        }
    return None


async def create_ai_ingredient(
    conn,
    suggested_name: str,
    suggested_unit: str,
    tenant_id,
    peso_unidad_gr: Optional[float] = None
) -> Optional[str]:
    """
    Creates a new ingredient inferred by Gemini during invoice scanning,
    then inserts standard purchase units for it based on the base unit pattern
    used across the existing catalog.

    Standard purchase units created by base unit:
      gr  → kg/1000 (default), lb/454
      ml  → botella/{peso} or botella/1000 (default), galon/3785, garrafa/5000
      und → no extra purchase units (buying by unit is already the base)

    Runs a pg_trgm similarity check first to prevent near-duplicate creation.
    The new ingredient is scoped to the tenant (tenant_id set) so it does not
    pollute the global catalog until an admin promotes it.

    Returns the UUID of the created (or existing duplicate) ingredient as a string,
    or None if creation failed.
    """
    try:
        async with conn.transaction():
            # pg_trgm duplicate guard: if a very similar name already exists, reuse it
            existing = await conn.fetchrow(
                """
                SELECT id
                FROM ingredients
                WHERE similarity(name, $1) > 0.75
                ORDER BY similarity(name, $1) DESC
                LIMIT 1
                """,
                suggested_name
            )

            if existing:
                logger.info(
                    f"AI ingredient '{suggested_name}' matches existing ingredient "
                    f"{existing['id']} — skipping creation"
                )
                return str(existing["id"])

            row = await conn.fetchrow(
                """
                INSERT INTO ingredients (name, unit, tenant_id, ai_generated, ai_generated_at)
                VALUES ($1, $2, $3, TRUE, NOW())
                RETURNING id
                """,
                suggested_name,
                suggested_unit,
                tenant_id
            )

            ingredient_id = row["id"]
            logger.info(
                f"AI-created ingredient '{suggested_name}' ({suggested_unit}) "
                f"for tenant {tenant_id} → id {ingredient_id}"
            )

            # Auto-create standard purchase units based on the base unit pattern
            purchase_units = _build_standard_purchase_units(suggested_unit, peso_unidad_gr)
            for pu in purchase_units:
                await conn.execute(
                    """
                    INSERT INTO ingredient_purchase_units
                      (ingredient_id, purchase_unit, purchase_unit_label,
                       conversion_factor, is_default, is_active)
                    VALUES ($1, $2, $3, $4, $5, TRUE)
                    """,
                    ingredient_id,
                    pu["purchase_unit"],
                    pu["purchase_unit_label"],
                    pu["conversion_factor"],
                    pu["is_default"],
                )

            if purchase_units:
                logger.info(
                    f"Auto-created {len(purchase_units)} purchase units for "
                    f"ingredient {ingredient_id} ({suggested_unit})"
                )

            return str(ingredient_id)

    except Exception as e:
        logger.error(f"Failed to create AI ingredient '{suggested_name}': {e}")
        return None


def _build_standard_purchase_units(base_unit: str, peso_unidad_gr: Optional[float]) -> list:
    """
    Returns the standard purchase unit configurations for a new ingredient
    based on the base unit, mirroring the patterns used in the existing catalog.
    """
    if base_unit == "gr":
        units = [
            {"purchase_unit": "kg", "purchase_unit_label": "1 Kilogramo",
             "conversion_factor": 1000.0, "is_default": True},
            {"purchase_unit": "lb", "purchase_unit_label": "1 Libra",
             "conversion_factor": 454.0, "is_default": False},
        ]
        # If invoice reveals a specific unit weight, also add individual unit
        if peso_unidad_gr and 10 < peso_unidad_gr < 30000:
            units.append({
                "purchase_unit": "und",
                "purchase_unit_label": f"1 Unidad ({int(peso_unidad_gr)}g)",
                "conversion_factor": float(peso_unidad_gr),
                "is_default": False,
            })
        return units

    if base_unit == "ml":
        bottle_ml = float(peso_unidad_gr) if peso_unidad_gr and peso_unidad_gr > 0 else 1000.0
        if bottle_ml < 1000:
            label = f"Botella {int(bottle_ml)}ml"
        else:
            label = f"Botella {bottle_ml / 1000:.4g}L"
        return [
            {"purchase_unit": "botella", "purchase_unit_label": label,
             "conversion_factor": bottle_ml, "is_default": True},
            {"purchase_unit": "galon", "purchase_unit_label": "Galón 3785ml",
             "conversion_factor": 3785.0, "is_default": False},
            {"purchase_unit": "garrafa", "purchase_unit_label": "Garrafa 5L",
             "conversion_factor": 5000.0, "is_default": False},
        ]

    # "und" or unknown: no extra purchase units needed
    return []
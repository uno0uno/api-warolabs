from typing import List, Optional
from uuid import UUID
from fastapi import Request, Response, HTTPException
from app.database import get_db_connection
from app.core.middleware import require_valid_session
import logging
from app.core.exceptions import AuthenticationError
# Catalog of standard purchase units and their conversion to gr or ml base unit
# Mirrors PURCHASE_UNIT_CATALOG in ingredients_service.py
_CATALOG_TO_BASE: dict = {
    'kg':         {'factor': 1000,  'base': 'gr'},
    'libra':      {'factor': 500,   'base': 'gr'},
    'arroba':     {'factor': 12500, 'base': 'gr'},
    'bulto_25kg': {'factor': 25000, 'base': 'gr'},
    'lt':         {'factor': 1000,  'base': 'ml'},
    'botella':    {'factor': 750,   'base': 'ml'},
    'galon':      {'factor': 3785,  'base': 'ml'},
    'und':        {'factor': 1,     'base': 'und'},
}
from app.models.ingredient import (
    IngredientPurchaseUnit,
    IngredientPurchaseUnitCreate,
    IngredientPurchaseUnitUpdate,
    IngredientPurchaseUnitResponse,
    IngredientPurchaseUnitsListResponse
)

logger = logging.getLogger(__name__)


async def resolve_to_base_unit(
    conn,
    ingredient_id: UUID,
    quantity: float,
    unit: str
) -> tuple:
    """
    Converts a quantity+unit pair to the ingredient's base unit.

    - If unit == ingredient base unit → returns as-is.
    - If a matching entry exists in ingredient_purchase_units → applies conversion_factor.
    - If no conversion found → logs a warning and returns original values unchanged.

    Returns: (base_quantity: float, base_unit: str)
    """
    ing_row = await conn.fetchrow(
        "SELECT unit FROM ingredients WHERE id = $1",
        ingredient_id
    )
    if not ing_row:
        return float(quantity), unit

    base_unit = ing_row['unit']

    if unit == base_unit:
        return float(quantity), base_unit

    conv_row = await conn.fetchrow(
        """
        SELECT conversion_factor
        FROM ingredient_purchase_units
        WHERE ingredient_id = $1
          AND (purchase_unit_label = $2 OR purchase_unit = $2)
          AND is_active = TRUE
        ORDER BY is_default DESC
        LIMIT 1
        """,
        ingredient_id,
        unit
    )

    if conv_row and conv_row['conversion_factor']:
        base_quantity = float(quantity) * float(conv_row['conversion_factor'])
        logger.info(
            f"Unit conversion: {quantity} {unit} → {base_quantity} {base_unit} "
            f"(factor={conv_row['conversion_factor']}, ingredient={ingredient_id})"
        )
        return base_quantity, base_unit

    logger.warning(
        f"No conversion found for unit '{unit}' on ingredient {ingredient_id} "
        f"(base_unit='{base_unit}'). Saving without conversion."
    )
    return float(quantity), unit


async def resolve_recipe_quantity_to_base_unit(
    conn,
    ingredient_id: UUID,
    recipe_qty: float,
    recipe_unit: str,
) -> float:
    """
    Converts a recipe quantity to the ingredient's base unit.

    Supported conversions (requires unit_weight_gr > 0 on the ingredient):
    - recipe in gr/ml, stock in und → qty / unit_weight_gr  (e.g. 360 gr ÷ 180 gr/und = 2 und)
    - recipe in und, stock in gr/ml → qty * unit_weight_gr  (e.g. 1 und × 750 ml/und = 750 ml)

    Otherwise returns recipe_qty unchanged.
    """
    row = await conn.fetchrow(
        "SELECT unit, unit_weight_gr FROM ingredients WHERE id = $1",
        ingredient_id
    )
    if not row:
        return recipe_qty
    base_unit = row["unit"]
    if recipe_unit == base_unit:
        return recipe_qty
    weight = row["unit_weight_gr"]
    if weight and float(weight) > 0:
        w = float(weight)
        if recipe_unit in ("gr", "ml") and base_unit == "und":
            converted = recipe_qty / w
            logger.info(
                f"Recipe unit conversion: {recipe_qty} {recipe_unit} → {converted:.4f} und "
                f"(unit_weight_gr={weight}, ingredient={ingredient_id})"
            )
            return converted
        if recipe_unit == "und" and base_unit in ("gr", "ml"):
            converted = recipe_qty * w
            logger.info(
                f"Recipe unit conversion: {recipe_qty} und → {converted:.4f} {base_unit} "
                f"(unit_weight_gr={weight}, ingredient={ingredient_id})"
            )
            return converted
        # Two-step: catalog unit (lt, kg, botella…) → gr/ml → und
        # e.g. 2 lt on a und ingredient with unit_weight_gr=750 ml/und
        #      → 2 * 1000 ml = 2000 ml → 2000 / 750 = 2.667 und
        catalog_entry = _CATALOG_TO_BASE.get(recipe_unit)
        if catalog_entry and base_unit == "und" and catalog_entry["base"] in ("gr", "ml"):
            qty_base = recipe_qty * catalog_entry["factor"]
            converted = qty_base / w
            logger.info(
                f"Recipe unit conversion (2-step): {recipe_qty} {recipe_unit} "
                f"→ {qty_base} {catalog_entry['base']} → {converted:.4f} und "
                f"(catalog_factor={catalog_entry['factor']}, unit_weight_gr={weight}, ingredient={ingredient_id})"
            )
            return converted
    logger.warning(
        f"No recipe unit conversion for '{recipe_unit}' → '{base_unit}' on ingredient {ingredient_id}. "
        f"Returning recipe_qty unchanged."
    )
    return recipe_qty


async def get_purchase_units_by_ingredient(
    request: Request,
    response: Response,
    ingredient_id: UUID,
    active_only: bool = True
) -> IngredientPurchaseUnitsListResponse:
    """
    Get all purchase unit configurations for a specific ingredient
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            query = """
                SELECT
                    ipu.id,
                    ipu.ingredient_id,
                    ipu.purchase_unit,
                    ipu.purchase_unit_label,
                    CAST(ipu.conversion_factor AS float) as conversion_factor,
                    CAST(ipu.unit_cost AS float) as unit_cost,
                    ipu.is_default,
                    ipu.is_active,
                    ipu.notes,
                    ipu.created_at,
                    ipu.updated_at,
                    i.name as ingredient_name,
                    i.unit as ingredient_base_unit
                FROM ingredient_purchase_units ipu
                JOIN ingredients i ON ipu.ingredient_id = i.id
                WHERE ipu.ingredient_id = $1
            """

            params = [ingredient_id]

            if active_only:
                query += " AND ipu.is_active = true"

            query += " ORDER BY ipu.is_default DESC, ipu.conversion_factor ASC"

            rows = await conn.fetch(query, *params)

            purchase_units = [IngredientPurchaseUnit(**dict(row)) for row in rows]

            return IngredientPurchaseUnitsListResponse(
                success=True,
                total=len(purchase_units),
                data=purchase_units
            )

    except AuthenticationError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        logger.error(f"Error fetching purchase units: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error fetching purchase units: {str(e)}")


async def get_purchase_unit_by_id(
    request: Request,
    response: Response,
    purchase_unit_id: UUID
) -> IngredientPurchaseUnitResponse:
    """
    Get a specific purchase unit configuration by ID
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            query = """
                SELECT
                    ipu.id,
                    ipu.ingredient_id,
                    ipu.purchase_unit,
                    ipu.purchase_unit_label,
                    CAST(ipu.conversion_factor AS float) as conversion_factor,
                    CAST(ipu.unit_cost AS float) as unit_cost,
                    ipu.is_default,
                    ipu.is_active,
                    ipu.notes,
                    ipu.created_at,
                    ipu.updated_at,
                    i.name as ingredient_name,
                    i.unit as ingredient_base_unit
                FROM ingredient_purchase_units ipu
                JOIN ingredients i ON ipu.ingredient_id = i.id
                WHERE ipu.id = $1
            """

            row = await conn.fetchrow(query, purchase_unit_id)

            if not row:
                raise HTTPException(status_code=404, detail="Purchase unit not found")

            purchase_unit = IngredientPurchaseUnit(**dict(row))

            return IngredientPurchaseUnitResponse(
                success=True,
                data=purchase_unit
            )

    except HTTPException:
        raise
    except AuthenticationError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        logger.error(f"Error fetching purchase unit: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error fetching purchase unit: {str(e)}")


async def create_purchase_unit(
    request: Request,
    response: Response,
    purchase_unit_data: IngredientPurchaseUnitCreate
) -> IngredientPurchaseUnitResponse:
    """
    Create a new purchase unit configuration for an ingredient
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Verify ingredient exists and belongs to tenant or is global
            ingredient_check = await conn.fetchrow(
                """
                SELECT id FROM ingredients
                WHERE id = $1 AND (tenant_id = $2 OR tenant_id IS NULL)
                """,
                purchase_unit_data.ingredient_id,
                tenant_id
            )

            if not ingredient_check:
                raise HTTPException(status_code=404, detail="Ingredient not found")

            # Insert new purchase unit and return all fields in one query
            insert_query = """
                INSERT INTO ingredient_purchase_units (
                    ingredient_id,
                    purchase_unit,
                    purchase_unit_label,
                    conversion_factor,
                    unit_cost,
                    is_default,
                    is_active,
                    notes
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (ingredient_id, purchase_unit, purchase_unit_label)
                DO UPDATE SET
                    conversion_factor = EXCLUDED.conversion_factor,
                    unit_cost = COALESCE(EXCLUDED.unit_cost, ingredient_purchase_units.unit_cost),
                    is_default = EXCLUDED.is_default,
                    is_active = EXCLUDED.is_active,
                    notes = COALESCE(EXCLUDED.notes, ingredient_purchase_units.notes)
                RETURNING
                    id,
                    ingredient_id,
                    purchase_unit,
                    purchase_unit_label,
                    CAST(conversion_factor AS float) as conversion_factor,
                    CAST(unit_cost AS float) as unit_cost,
                    is_default,
                    is_active,
                    notes,
                    created_at,
                    updated_at
            """

            row = await conn.fetchrow(
                insert_query,
                purchase_unit_data.ingredient_id,
                purchase_unit_data.purchase_unit,
                purchase_unit_data.purchase_unit_label,
                purchase_unit_data.conversion_factor,
                purchase_unit_data.unit_cost,
                purchase_unit_data.is_default,
                purchase_unit_data.is_active,
                purchase_unit_data.notes
            )

            purchase_unit = IngredientPurchaseUnit(**dict(row))
            return IngredientPurchaseUnitResponse(success=True, data=purchase_unit)

    except HTTPException:
        raise
    except AuthenticationError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        logger.error(f"Error creating purchase unit: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error creating purchase unit: {str(e)}")


async def update_purchase_unit(
    request: Request,
    response: Response,
    purchase_unit_id: UUID,
    purchase_unit_data: IngredientPurchaseUnitUpdate
) -> IngredientPurchaseUnitResponse:
    """
    Update an existing purchase unit configuration
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Check if purchase unit exists
            existing = await conn.fetchrow(
                "SELECT id FROM ingredient_purchase_units WHERE id = $1",
                purchase_unit_id
            )

            if not existing:
                raise HTTPException(status_code=404, detail="Purchase unit not found")

            # Build update query dynamically based on provided fields
            update_fields = []
            params = []
            param_count = 1

            if purchase_unit_data.purchase_unit is not None:
                update_fields.append(f"purchase_unit = ${param_count}")
                params.append(purchase_unit_data.purchase_unit)
                param_count += 1

            if purchase_unit_data.purchase_unit_label is not None:
                update_fields.append(f"purchase_unit_label = ${param_count}")
                params.append(purchase_unit_data.purchase_unit_label)
                param_count += 1

            if purchase_unit_data.conversion_factor is not None:
                update_fields.append(f"conversion_factor = ${param_count}")
                params.append(purchase_unit_data.conversion_factor)
                param_count += 1

            if purchase_unit_data.unit_cost is not None:
                update_fields.append(f"unit_cost = ${param_count}")
                params.append(purchase_unit_data.unit_cost)
                param_count += 1

            if purchase_unit_data.is_default is not None:
                update_fields.append(f"is_default = ${param_count}")
                params.append(purchase_unit_data.is_default)
                param_count += 1

            if purchase_unit_data.is_active is not None:
                update_fields.append(f"is_active = ${param_count}")
                params.append(purchase_unit_data.is_active)
                param_count += 1

            if purchase_unit_data.notes is not None:
                update_fields.append(f"notes = ${param_count}")
                params.append(purchase_unit_data.notes)
                param_count += 1

            if not update_fields:
                raise HTTPException(status_code=400, detail="No fields to update")

            params.append(purchase_unit_id)
            update_query = f"""
                UPDATE ingredient_purchase_units
                SET {', '.join(update_fields)}
                WHERE id = ${param_count}
            """

            await conn.execute(update_query, *params)

            # Fetch updated purchase unit
            return await get_purchase_unit_by_id(request, response, purchase_unit_id)

    except HTTPException:
        raise
    except AuthenticationError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        logger.error(f"Error updating purchase unit: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error updating purchase unit: {str(e)}")


async def delete_purchase_unit(
    request: Request,
    response: Response,
    purchase_unit_id: UUID
) -> dict:
    """
    Delete a purchase unit configuration
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            result = await conn.execute(
                "DELETE FROM ingredient_purchase_units WHERE id = $1",
                purchase_unit_id
            )

            if result == "DELETE 0":
                raise HTTPException(status_code=404, detail="Purchase unit not found")

            return {"success": True, "message": "Purchase unit deleted successfully"}

    except HTTPException:
        raise
    except AuthenticationError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        logger.error(f"Error deleting purchase unit: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error deleting purchase unit: {str(e)}")


async def get_all_purchase_units(
    request: Request,
    response: Response,
    page: int = 1,
    limit: int = 100,
    search: Optional[str] = None,
    ingredient_id: Optional[UUID] = None,
    active_only: bool = True
) -> IngredientPurchaseUnitsListResponse:
    """
    Get all purchase units with optional filtering
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            query = """
                SELECT
                    ipu.id,
                    ipu.ingredient_id,
                    ipu.purchase_unit,
                    ipu.purchase_unit_label,
                    CAST(ipu.conversion_factor AS float) as conversion_factor,
                    CAST(ipu.unit_cost AS float) as unit_cost,
                    ipu.is_default,
                    ipu.is_active,
                    ipu.notes,
                    ipu.created_at,
                    ipu.updated_at,
                    i.name as ingredient_name,
                    i.unit as ingredient_base_unit
                FROM ingredient_purchase_units ipu
                JOIN ingredients i ON ipu.ingredient_id = i.id
                WHERE 1=1
            """

            params = []
            param_count = 1

            if ingredient_id:
                query += f" AND ipu.ingredient_id = ${param_count}"
                params.append(ingredient_id)
                param_count += 1

            if active_only:
                query += " AND ipu.is_active = true"

            if search:
                query += f" AND (i.name ILIKE ${param_count} OR ipu.purchase_unit_label ILIKE ${param_count})"
                params.append(f"%{search}%")
                param_count += 1

            # Get total count
            count_query = f"SELECT COUNT(*) FROM ({query}) as count_query"
            total = await conn.fetchval(count_query, *params)

            # Add pagination
            query += " ORDER BY i.name, ipu.is_default DESC, ipu.conversion_factor ASC"
            offset = (page - 1) * limit
            query += f" LIMIT ${param_count} OFFSET ${param_count + 1}"
            params.extend([limit, offset])

            rows = await conn.fetch(query, *params)

            purchase_units = [IngredientPurchaseUnit(**dict(row)) for row in rows]

            return IngredientPurchaseUnitsListResponse(
                success=True,
                total=total,
                data=purchase_units
            )

    except AuthenticationError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        logger.error(f"Error fetching purchase units: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error fetching purchase units: {str(e)}")

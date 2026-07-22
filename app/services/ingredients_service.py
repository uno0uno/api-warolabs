from typing import List, Optional, Dict, Any
from uuid import UUID
from fastapi import Request, Response, HTTPException
from app.database import get_db_connection
from app.core.middleware import require_valid_session
import logging
import asyncpg
from app.core.exceptions import AuthenticationError
from app.models.ingredient import Ingredient, IngredientsListResponse, TenantIngredientCreate, TenantIngredientUpdate
from app.services.warehouse_categories_service import (
    list_warehouse_categories,
    resolve_assignable_warehouse_category,
)
from app.services.billing_service import check_plan_quota_growth

logger = logging.getLogger(__name__)

FOOD_UNITS = frozenset({"gr", "ml", "kg", "und", "lt"})
INGREDIENT_TYPES = frozenset({"food", "service", "supply"})


def _normalize_ingredient_type(type_val: Optional[str]) -> str:
    ingredient_type = (type_val or "food").strip().lower()
    if ingredient_type not in INGREDIENT_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid type '{type_val}'. Must be one of: {', '.join(sorted(INGREDIENT_TYPES))}",
        )
    return ingredient_type


def _validate_unit_for_type(unit: str, ingredient_type: str) -> None:
    if ingredient_type == "service":
        if unit not in ("hr", "und"):
            raise HTTPException(
                status_code=422,
                detail="Service ingredients must use unit 'hr' (per hour) or 'und' (per trip/fixed service).",
            )
        return
    if ingredient_type == "supply":
        if unit != "und":
            raise HTTPException(
                status_code=422,
                detail="Supply ingredients must use unit 'und'.",
            )
        return
    if unit not in FOOD_UNITS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid unit '{unit}'. Must be one of: {', '.join(sorted(FOOD_UNITS))}",
        )


# Catalog of allowed purchase units. Label and conversion_factor are resolved
# server-side — the client only sends the key. Add new units here as needed.
PURCHASE_UNIT_CATALOG: Dict[str, Dict[str, Any]] = {
    # Weight (base unit: gr)
    'kg':          {'label': 'Kilogramo',    'conversion_factor': 1000,  'compatible_units': {'gr'}},
    'libra':       {'label': 'Libra',        'conversion_factor': 500,   'compatible_units': {'gr'}},
    'arroba':      {'label': 'Arroba',       'conversion_factor': 12500, 'compatible_units': {'gr'}},
    'bulto_25kg':  {'label': 'Bulto (25 kg)','conversion_factor': 25000, 'compatible_units': {'gr'}},
    # Volume (base unit: ml)
    'lt':          {'label': 'Litro',        'conversion_factor': 1000,  'compatible_units': {'ml'}},
    'botella':     {'label': 'Botella',      'conversion_factor': 750,   'compatible_units': {'ml'}},
    'galon':       {'label': 'Galón',        'conversion_factor': 3785,  'compatible_units': {'ml'}},
    # Piece (base unit: und)
    'und':         {'label': 'Unidad',       'conversion_factor': 1,     'compatible_units': {'und'}},
    # Time (base unit: hr) — e.g. monthly retainer packs
    'hr':          {'label': 'Hora',         'conversion_factor': 1,     'compatible_units': {'hr'}},
}


async def get_ingredient_categories(
    conn,
    tenant_id: UUID,
    search: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Compatibility alias backed by the managed warehouse category catalog."""
    return await list_warehouse_categories(conn, tenant_id, search, limit)


async def resolve_ingredients_by_warehouse_categories(
    conn,
    tenant_id: UUID,
    category_ids: List[UUID],
    exclude_ingredient_ids: Optional[List[UUID]] = None,
    exclude_resale: bool = False,
) -> Dict[str, Any]:
    """Resolve visible active ingredients for ordered warehouse categories."""
    ordered_category_ids = list(dict.fromkeys(category_ids))
    excluded_ids = list(dict.fromkeys(exclude_ingredient_ids or []))

    rows = await conn.fetch(
        """
        WITH requested_categories AS (
            SELECT category_id, MIN(position)::int AS position
            FROM unnest($2::uuid[]) WITH ORDINALITY AS requested(category_id, position)
            GROUP BY category_id
        ),
        visible_categories AS (
            SELECT requested.category_id, requested.position, wc.id IS NOT NULL AS is_available
            FROM requested_categories requested
            LEFT JOIN warehouse_categories wc
              ON wc.id = requested.category_id
             AND wc.is_active = TRUE
             AND (wc.tenant_id IS NULL OR wc.tenant_id = $1)
        )
        SELECT
            category.category_id,
            category.position,
            category.is_available,
            ingredient.id AS ingredient_id,
            ingredient.name,
            ingredient.unit,
            ingredient.warehouse_category_id
        FROM visible_categories category
        LEFT JOIN ingredients ingredient
          ON ingredient.warehouse_category_id = category.category_id
         AND category.is_available
         AND ingredient.is_active = TRUE
         AND (ingredient.tenant_id IS NULL OR ingredient.tenant_id = $1)
         AND NOT (ingredient.id = ANY($3::uuid[]))
         AND ($4::boolean IS FALSE OR COALESCE(ingredient.is_resale, FALSE) = FALSE)
        ORDER BY category.position, LOWER(ingredient.name) NULLS LAST,
                 ingredient.name NULLS LAST, ingredient.id
        """,
        tenant_id,
        ordered_category_ids,
        excluded_ids,
        exclude_resale,
    )

    ingredients: List[Dict[str, Any]] = []
    empty_category_ids: List[UUID] = []
    unavailable_category_ids: List[UUID] = []
    seen_ingredient_ids = set()

    for row in rows:
        category_id = row["category_id"]
        if not row["is_available"]:
            unavailable_category_ids.append(category_id)
            continue
        if row["ingredient_id"] is None:
            empty_category_ids.append(category_id)
            continue
        if row["ingredient_id"] in seen_ingredient_ids:
            continue
        seen_ingredient_ids.add(row["ingredient_id"])
        ingredients.append({
            "ingredient_id": row["ingredient_id"],
            "name": row["name"],
            "unit": row["unit"],
            "warehouse_category_id": row["warehouse_category_id"],
        })

    return {
        "ingredients": ingredients,
        "empty_category_ids": empty_category_ids,
        "unavailable_category_ids": unavailable_category_ids,
    }


def resolve_purchase_units(purchase_units: list, base_unit: str) -> list:
    """
    Validate each purchase_unit key against the catalog and resolve
    label + conversion_factor server-side. Raises 422 on unknown or
    incompatible keys.
    """
    resolved = []
    for pu in purchase_units:
        entry = PURCHASE_UNIT_CATALOG.get(pu.purchase_unit)
        if not entry:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown purchase unit '{pu.purchase_unit}'. Allowed: {', '.join(sorted(PURCHASE_UNIT_CATALOG))}"
            )
        if base_unit not in entry['compatible_units']:
            raise HTTPException(
                status_code=422,
                detail=f"Purchase unit '{pu.purchase_unit}' is not compatible with base unit '{base_unit}'"
            )
        resolved.append({
            'purchase_unit': pu.purchase_unit,
            'label': entry['label'],
            'conversion_factor': entry['conversion_factor'],
            'is_default': pu.is_default,
        })
    return resolved


async def _copy_parent_purchase_units(conn, parent_id: UUID, ingredient_id: UUID) -> None:
    """Seed a new variant ingredient with independent copies of the parent's purchase units."""
    parent_units = await conn.fetch(
        """
        SELECT purchase_unit, purchase_unit_label, conversion_factor, unit_cost,
               is_default, is_active, notes
        FROM ingredient_purchase_units
        WHERE ingredient_id = $1
        ORDER BY is_default DESC, purchase_unit_label
        """,
        parent_id,
    )
    for pu in parent_units:
        await conn.execute(
            """
            INSERT INTO ingredient_purchase_units
                (ingredient_id, purchase_unit, purchase_unit_label, conversion_factor,
                 unit_cost, is_default, is_active, notes)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            ingredient_id,
            pu["purchase_unit"],
            pu["purchase_unit_label"],
            pu["conversion_factor"],
            pu["unit_cost"],
            pu["is_default"],
            pu["is_active"],
            pu["notes"],
        )


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
    tenant_only: Optional[bool] = None,
    show_archived: Optional[bool] = None,
    unit: Optional[str] = None,
    has_cost: Optional[bool] = None,
    has_unit_weight: Optional[bool] = None,
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
                    i.warehouse_category_id,
                    i.type,
                    i.description,
                    CAST(i.minimum_order_quantity AS float) as minimum_order_quantity,
                    CAST(i.unit_weight_gr AS float) as unit_weight_gr,
                    i.unit_weight_unit,
                    i.is_resale,
                    CAST(i.costo_unitario AS float) as costo_unitario,
                    i.created_at,
                    i.updated_at,
                    CAST(COALESCE(tsp.unit_price, tim.cost_per_unit) AS float) as price,
                    tsp.supplier_id,
                    igh.base_id::text   AS hierarchy_base_id,
                    hb.name             AS hierarchy_base_name,
                    COALESCE((
                        SELECT COUNT(*)::int FROM ingredient_global_hierarchy
                        WHERE base_id = i.id
                    ), 0) AS has_variants,
                    (i.tenant_id IS NOT NULL) AS is_custom,
                    parent_i.name       AS parent_name,
                    (
                        SELECT purchase_unit_label
                        FROM ingredient_purchase_units
                        WHERE ingredient_id = i.id AND is_default = TRUE AND is_active = TRUE
                        LIMIT 1
                    ) AS default_purchase_unit_label,
                    (
                        SELECT CAST(conversion_factor AS float)
                        FROM ingredient_purchase_units
                        WHERE ingredient_id = i.id AND is_default = TRUE AND is_active = TRUE
                        LIMIT 1
                    ) AS default_purchase_unit_factor
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
                LEFT JOIN ingredients parent_i ON parent_i.id = i.parent_id
                WHERE (i.tenant_id IS NULL OR i.tenant_id = $1) AND i.is_active = TRUE
            """

            count_query = "SELECT COUNT(*) FROM ingredients WHERE (tenant_id IS NULL OR tenant_id = $1) AND is_active = TRUE"

            # Separate params for base query (includes tenant_id) and count query (also includes tenant_id)
            base_params = [tenant_id]
            count_params = [tenant_id]
            base_param_count = 2
            count_param_count = 2

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
                # Exclude global variants (those with a base in ingredient_global_hierarchy)
                # and exclude all tenant custom ingredients (never bases for global hierarchy)
                base_query += " AND i.tenant_id IS NULL AND NOT EXISTS (SELECT 1 FROM ingredient_global_hierarchy WHERE variant_id = i.id)"
                count_query += " AND tenant_id IS NULL AND NOT EXISTS (SELECT 1 FROM ingredient_global_hierarchy WHERE variant_id = id)"

            if tenant_only:
                # Return only tenant-scoped custom ingredients (excludes global catalog)
                base_query += " AND i.tenant_id IS NOT NULL"
                count_query += " AND tenant_id IS NOT NULL"

            if show_archived:
                # Override the is_active=TRUE filter added in the base WHERE clause
                base_query = base_query.replace("AND i.is_active = TRUE", "AND i.is_active = FALSE")
                count_query = count_query.replace("AND is_active = TRUE", "AND is_active = FALSE")

            if unit:
                base_query += f" AND LOWER(i.unit) = LOWER(${base_param_count})"
                count_query += f" AND LOWER(unit) = LOWER(${count_param_count})"
                base_params.append(unit)
                count_params.append(unit)
                base_param_count += 1
                count_param_count += 1

            if has_cost is True:
                base_query += " AND i.costo_unitario IS NOT NULL AND i.costo_unitario > 0"
                count_query += " AND costo_unitario IS NOT NULL AND costo_unitario > 0"
            elif has_cost is False:
                base_query += " AND (i.costo_unitario IS NULL OR i.costo_unitario <= 0)"
                count_query += " AND (costo_unitario IS NULL OR costo_unitario <= 0)"

            if has_unit_weight is True:
                base_query += " AND i.unit_weight_gr IS NOT NULL AND i.unit_weight_gr > 0"
                count_query += " AND unit_weight_gr IS NOT NULL AND unit_weight_gr > 0"
            elif has_unit_weight is False:
                base_query += " AND (i.unit_weight_gr IS NULL OR i.unit_weight_gr <= 0)"
                count_query += " AND (unit_weight_gr IS NULL OR unit_weight_gr <= 0)"

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


async def create_tenant_ingredient(
    conn,
    tenant_id: UUID,
    data: TenantIngredientCreate,
) -> Dict[str, Any]:
    """
    Creates a custom ingredient scoped to the given tenant.

    - name: trimmed, unique within tenant (enforced by ingredients_name_tenant_unique index)
    - unit: food → gr/ml/kg/und/lt; service → hr or und; supply → und
    - parent_id (optional): must reference a global ingredient (tenant_id IS NULL)
    """
    name = data.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Ingredient name cannot be empty")

    ingredient_type = _normalize_ingredient_type(data.type)
    _validate_unit_for_type(data.unit, ingredient_type)

    if (data.is_resale or False) and data.unit != "und":
        raise HTTPException(
            status_code=422,
            detail="Resale ingredients must use unit 'und'. Change the unit to 'und' before marking as resale."
        )

    parent_uuid = None
    parent_name = None
    if data.parent_id:
        try:
            parent_uuid = UUID(data.parent_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="parent_id must be a valid UUID")

        parent_row = await conn.fetchrow(
            "SELECT id, name FROM ingredients WHERE id = $1 AND tenant_id IS NULL",
            parent_uuid,
        )
        if not parent_row:
            raise HTTPException(
                status_code=422,
                detail="parent_id not found or not a global ingredient"
            )
        parent_name = parent_row["name"]

    category = await resolve_assignable_warehouse_category(
        conn,
        tenant_id,
        data.warehouse_category_id,
        data.category,
    )

    await check_plan_quota_growth(conn, tenant_id, "tenant_ingredients")

    try:
        row = await conn.fetchrow(
            """
            INSERT INTO ingredients (
                name, unit, type, category, warehouse_category_id, costo_unitario,
                parent_id, tenant_id, is_resale, unit_weight_gr, unit_weight_unit
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            RETURNING id::text, name, unit, type, category, warehouse_category_id, costo_unitario,
                      parent_id::text, tenant_id::text, is_resale, unit_weight_gr, unit_weight_unit, created_at
            """,
            name,
            data.unit,
            ingredient_type,
            category["name"] if category else None,
            category["id"] if category else None,
            data.costo_unitario,
            parent_uuid,
            tenant_id,
            data.is_resale or False,
            data.unit_weight_gr if data.unit in ("und", "gr", "ml") else None,
            (data.unit_weight_unit or "gr") if data.unit in ("und", "gr", "ml") else None,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=409,
            detail=f"An ingredient named '{name}' already exists for this restaurant."
        )

    ingredient_id_text = row["id"]
    result = dict(row)
    result["is_custom"] = True
    if parent_uuid:
        result["parent_name"] = parent_name

    # Insert purchase units within the same transaction
    from uuid import UUID as _UUID
    ingredient_uuid = _UUID(ingredient_id_text)

    if data.purchase_units:
        resolved = resolve_purchase_units(data.purchase_units, data.unit)
        for pu in resolved:
            await conn.execute(
                """
                INSERT INTO ingredient_purchase_units
                    (ingredient_id, purchase_unit, purchase_unit_label, conversion_factor, is_default, is_active)
                VALUES ($1, $2, $3, $4, $5, true)
                """,
                ingredient_uuid,
                pu['purchase_unit'],
                pu['label'],
                pu['conversion_factor'],
                pu['is_default'],
            )
    elif parent_uuid:
        await _copy_parent_purchase_units(conn, parent_uuid, ingredient_uuid)

    return result


async def update_tenant_ingredient(
    conn,
    tenant_id: UUID,
    ingredient_id: UUID,
    data: TenantIngredientUpdate,
) -> Dict[str, Any]:
    """
    Updates a tenant-scoped custom ingredient.
    Only fields provided (non-None) are updated.
    tenant_id guard ensures tenants can only edit their own ingredients.
    """
    row = await conn.fetchrow(
        "SELECT id, name, unit, type, category, warehouse_category_id, costo_unitario, parent_id::text FROM ingredients WHERE id = $1 AND tenant_id = $2",
        ingredient_id,
        tenant_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Ingredient not found or not owned by this restaurant")

    updates: Dict[str, Any] = {}

    if data.name is not None:
        name = data.name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="Ingredient name cannot be empty")
        updates["name"] = name

    if data.unit is not None:
        effective_type = _normalize_ingredient_type(row["type"])
        _validate_unit_for_type(data.unit, effective_type)
        updates["unit"] = data.unit

    category_id_unchanged = (
        data.warehouse_category_id is not None
        and data.warehouse_category_id == row["warehouse_category_id"]
    )
    if (data.warehouse_category_id is not None or data.category is not None) and not category_id_unchanged:
        category = await resolve_assignable_warehouse_category(
            conn,
            tenant_id,
            data.warehouse_category_id,
            data.category,
        )
        updates["category"] = category["name"] if category else None
        updates["warehouse_category_id"] = category["id"] if category else None

    if data.costo_unitario is not None:
        updates["costo_unitario"] = data.costo_unitario

    if data.is_resale is not None:
        updates["is_resale"] = data.is_resale

    if data.unit_weight_gr is not None:
        updates["unit_weight_gr"] = data.unit_weight_gr
    if data.unit_weight_unit is not None:
        updates["unit_weight_unit"] = data.unit_weight_unit

    # Resale guard: is_resale=True requires unit='und'
    # Check the effective unit: either the one being set now, or the existing one in DB
    effective_unit = updates.get("unit") or row["unit"]
    effective_is_resale = updates.get("is_resale")
    if effective_is_resale is None:
        effective_is_resale = False  # not being changed; existing value doesn't block
    if effective_is_resale and effective_unit != "und":
        raise HTTPException(
            status_code=422,
            detail="Resale ingredients must use unit 'und'. Change the unit to 'und' before marking as resale."
        )
    # Also block changing unit away from 'und' on an already-resale ingredient
    if "unit" in updates and updates["unit"] != "und":
        current_is_resale = await conn.fetchval(
            "SELECT is_resale FROM ingredients WHERE id = $1", ingredient_id
        )
        if current_is_resale:
            raise HTTPException(
                status_code=422,
                detail="Cannot change unit on a resale ingredient. Disable resale first."
            )

    # parent_id: empty string means clear it, a UUID string sets it
    parent_name = None
    if data.parent_id is not None:
        if data.parent_id == "":
            updates["parent_id"] = None
        else:
            try:
                parent_uuid = UUID(data.parent_id)
            except ValueError:
                raise HTTPException(status_code=422, detail="parent_id must be a valid UUID")
            parent_row = await conn.fetchrow(
                "SELECT id, name FROM ingredients WHERE id = $1 AND tenant_id IS NULL",
                parent_uuid,
            )
            if not parent_row:
                raise HTTPException(status_code=422, detail="parent_id not found or not a global ingredient")
            updates["parent_id"] = parent_uuid
            parent_name = parent_row["name"]

    if not updates:
        raise HTTPException(status_code=422, detail="No fields to update")

    set_clauses = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(updates))
    values = list(updates.values())

    try:
        updated = await conn.fetchrow(
            f"""
            UPDATE ingredients SET {set_clauses}, updated_at = NOW()
            WHERE id = $1
            RETURNING id::text, name, unit, category, warehouse_category_id,
                      costo_unitario, parent_id::text, tenant_id::text, updated_at
            """,
            ingredient_id,
            *values,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=409,
            detail=f"An ingredient named '{updates.get('name', row['name'])}' already exists for this restaurant."
        )

    result = dict(updated)
    result["is_custom"] = True
    if parent_name:
        result["parent_name"] = parent_name

    # Insert purchase units only if provided and ingredient has none yet
    if data.purchase_units:
        existing_count = await conn.fetchval(
            "SELECT COUNT(*) FROM ingredient_purchase_units WHERE ingredient_id = $1",
            ingredient_id,
        )
        if existing_count == 0:
            current_unit = updates.get("unit") or row["unit"] or ""
            resolved = resolve_purchase_units(data.purchase_units, current_unit)
            for pu in resolved:
                await conn.execute(
                    """
                    INSERT INTO ingredient_purchase_units
                        (ingredient_id, purchase_unit, purchase_unit_label, conversion_factor, is_default, is_active)
                    VALUES ($1, $2, $3, $4, $5, true)
                    """,
                    ingredient_id,
                    pu['purchase_unit'],
                    pu['label'],
                    pu['conversion_factor'],
                    pu['is_default'],
                )

    return result


async def create_ai_ingredient(
    conn,
    suggested_name: str,
    suggested_unit: str,
    tenant_id: Optional[UUID] = None, # No longer used for scoping new ingredients, kept for signature compatibility
    peso_unidad_gr: Optional[float] = None,
    suggested_base_name: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Creates a new ingredient inferred by Gemini during invoice scanning.
    AI-generated ingredients are now ALWAYS created globally (tenant_id = NULL).

    If suggested_base_name is provided, it ensures the base ingredient exists
    and creates a link in ingredient_global_hierarchy.
    """
    try:
        async with conn.transaction():
            # 1. Handle Hierarchy Base if suggested
            base_id = None
            if suggested_base_name:
                # Search for an existing global base ingredient
                base_row = await conn.fetchrow("""
                    SELECT id FROM ingredients
                    WHERE (LOWER(name) = LOWER($1) OR similarity(name, $1) > 0.8)
                    AND tenant_id IS NULL
                    ORDER BY similarity(name, $1) DESC
                    LIMIT 1
                """, suggested_base_name)

                if base_row:
                    base_id = base_row["id"]
                else:
                    # Create new global base ingredient
                    base_row = await conn.fetchrow("""
                        INSERT INTO ingredients (name, unit, tenant_id, ai_generated, ai_generated_at)
                        VALUES ($1, $2, NULL, TRUE, NOW())
                        RETURNING id
                    """, suggested_base_name, suggested_unit)
                    base_id = base_row["id"]
                    logger.info(f"Created new global base ingredient: {suggested_base_name}")

            # 2. Check for existing variant/ingredient (Global)
            existing = await conn.fetchrow("""
                SELECT id FROM ingredients
                WHERE (LOWER(name) = LOWER($1) OR similarity(name, $1) > 0.8)
                AND tenant_id IS NULL
                ORDER BY similarity(name, $1) DESC
                LIMIT 1
            """, suggested_name)

            if existing:
                ingredient_id = existing["id"]
                logger.info(f"AI ingredient '{suggested_name}' matches existing global ingredient {ingredient_id}")
            else:
                # 3. Create new global variant ingredient
                row = await conn.fetchrow("""
                    INSERT INTO ingredients (name, unit, tenant_id, ai_generated, ai_generated_at)
                    VALUES ($1, $2, NULL, TRUE, NOW())
                    RETURNING id
                """, suggested_name, suggested_unit)
                ingredient_id = row["id"]
                logger.info(f"Created new global AI ingredient: {suggested_name}")

                # 4. Auto-create standard purchase units
                purchase_units = _build_standard_purchase_units(suggested_unit, peso_unidad_gr)
                for pu in purchase_units:
                    await conn.execute("""
                        INSERT INTO ingredient_purchase_units
                          (ingredient_id, purchase_unit, purchase_unit_label,
                           conversion_factor, is_default, is_active)
                        VALUES ($1, $2, $3, $4, $5, TRUE)
                    """, ingredient_id, pu["purchase_unit"], pu["purchase_unit_label"],
                        pu["conversion_factor"], pu["is_default"])

            # 5. Link to base in hierarchy if needed
            if base_id and ingredient_id != base_id:
                await conn.execute("""
                    INSERT INTO ingredient_global_hierarchy (base_id, variant_id)
                    VALUES ($1, $2)
                    ON CONFLICT DO NOTHING
                """, base_id, ingredient_id)
                logger.info(f"Linked variant {ingredient_id} to base {base_id}")

            return {
                "id": str(ingredient_id),
                "name": suggested_name,
                "unit": suggested_unit,
                "type": "food" # Default for AI-created
            }

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


# ── Archive / Restore / Hard Delete ──────────────────────────────────────────

async def archive_tenant_ingredient(ingredient_id: str, tenant_id: str) -> Dict[str, Any]:
    """
    Archive a tenant ingredient: set is_active=False and atomically remove it from
    all active recipe/modifier definitions (product_recipes, base_recipe_templates,
    modifier_recipes, product_recipe_modifications, modifiers.ingredient_id).

    Historical records (order_item_ingredients, tenant_purchase_items,
    tenant_ingredient_movements, tenant_inventory) are preserved intact.

    Returns a summary of rows removed per table for the confirmation UI.
    """
    async with get_db_connection() as conn:
        ingredient = await conn.fetchrow(
            "SELECT id, name, is_active FROM ingredients WHERE id = $1 AND tenant_id = $2",
            ingredient_id, tenant_id,
        )
        if not ingredient:
            raise HTTPException(status_code=404, detail="Ingredient not found")
        if not ingredient["is_active"]:
            raise HTTPException(status_code=409, detail="Ingredient is already archived")

        # Count affected rows before deleting (for UI summary)
        pr  = await conn.fetchval("SELECT COUNT(*) FROM product_recipes              WHERE ingredient_id = $1", ingredient_id)
        brt = await conn.fetchval("SELECT COUNT(*) FROM base_recipe_templates        WHERE ingredient_id = $1", ingredient_id)
        mr  = await conn.fetchval("SELECT COUNT(*) FROM modifier_recipes             WHERE ingredient_id = $1", ingredient_id)
        prm = await conn.fetchval("SELECT COUNT(*) FROM product_recipe_modifications WHERE ingredient_id = $1", ingredient_id)
        mod = await conn.fetchval("SELECT COUNT(*) FROM modifiers                    WHERE ingredient_id = $1", ingredient_id)

        # Capture resale product IDs BEFORE the transaction (product_recipes rows still exist here)
        resale_rows = await conn.fetch(
            """SELECT p.id FROM product p
               JOIN product_recipes pr ON pr.product_id = p.id
               WHERE pr.ingredient_id = $1 AND p.is_resale = true AND p.tenant_id = $2""",
            ingredient_id, tenant_id,
        )
        resale_ids = [str(row["id"]) for row in resale_rows]

        async with conn.transaction():
            await conn.execute(
                "UPDATE ingredients SET is_active = FALSE, updated_at = NOW() WHERE id = $1",
                ingredient_id,
            )
            # Deactivate orphaned resale products before their recipe rows are deleted
            if resale_ids:
                await conn.execute(
                    "UPDATE product SET is_available = false WHERE id = ANY($1::uuid[]) AND is_resale = true",
                    resale_ids,
                )
            await conn.execute("DELETE FROM product_recipes              WHERE ingredient_id = $1", ingredient_id)
            await conn.execute("DELETE FROM base_recipe_templates        WHERE ingredient_id = $1", ingredient_id)
            await conn.execute("DELETE FROM modifier_recipes             WHERE ingredient_id = $1", ingredient_id)
            await conn.execute("DELETE FROM product_recipe_modifications WHERE ingredient_id = $1", ingredient_id)
            await conn.execute("UPDATE modifiers SET ingredient_id = NULL WHERE ingredient_id = $1", ingredient_id)

    return {
        "success": True,
        "archived": True,
        "removed": {
            "product_recipes": int(pr),
            "base_recipe_templates": int(brt),
            "modifier_recipes": int(mr),
            "product_recipe_modifications": int(prm),
            "modifiers_direct_link": int(mod),
            "resale_products_deactivated": len(resale_ids),
        },
    }


async def restore_tenant_ingredient(ingredient_id: str, tenant_id: str) -> Dict[str, Any]:
    """
    Restore an archived ingredient: set is_active=True.
    Does NOT automatically re-associate to recipes/modifiers — user must re-link manually.
    Returns 409 if another active ingredient with the same name already exists.
    """
    async with get_db_connection() as conn:
        ingredient = await conn.fetchrow(
            "SELECT id, name, is_active FROM ingredients WHERE id = $1 AND tenant_id = $2",
            ingredient_id, tenant_id,
        )
        if not ingredient:
            raise HTTPException(status_code=404, detail="Ingredient not found")
        if ingredient["is_active"]:
            raise HTTPException(status_code=409, detail="Ingredient is already active")

        conflict = await conn.fetchval(
            """SELECT id FROM ingredients
               WHERE tenant_id = $1 AND LOWER(name) = LOWER($2)
                 AND is_active = TRUE AND id != $3""",
            tenant_id, ingredient["name"], ingredient_id,
        )
        if conflict:
            raise HTTPException(
                status_code=409,
                detail=f"Ya existe un ingrediente activo con el nombre '{ingredient['name']}'. Cambia el nombre antes de restaurar.",
            )

        await conn.execute(
            "UPDATE ingredients SET is_active = TRUE, updated_at = NOW() WHERE id = $1",
            ingredient_id,
        )

    return {"success": True, "restored": True}


async def hard_delete_tenant_ingredient(ingredient_id: str, tenant_id: str) -> Dict[str, Any]:
    """
    Hard delete a tenant ingredient — only allowed when zero historical records exist.

    Checks 6 NO ACTION FK tables. If any has rows → 409 with suggest_archive=True.
    CASCADE FK tables (product_recipes, base_recipe_templates, ingredient_purchase_units,
    product_recipe_modifications, ingredient_global_hierarchy) are handled automatically by DB.
    """
    BLOCKING_TABLES: List[tuple] = [
        ("modifier_recipes",            "Recetas de modificadores"),
        ("order_item_ingredients",      "Historial de ventas"),
        ("tenant_ingredient_movements", "Movimientos de inventario"),
        ("tenant_inventory",            "Inventario activo"),
        ("tenant_purchase_items",       "Órdenes de compra"),
        ("tenant_supplier_prices",      "Precios de proveedores"),
    ]

    async with get_db_connection() as conn:
        ingredient = await conn.fetchrow(
            "SELECT id, name FROM ingredients WHERE id = $1 AND tenant_id = $2",
            ingredient_id, tenant_id,
        )
        if not ingredient:
            raise HTTPException(status_code=404, detail="Ingredient not found")

        blocking = []
        for table, label in BLOCKING_TABLES:
            count = await conn.fetchval(
                f"SELECT COUNT(*) FROM {table} WHERE ingredient_id = $1", ingredient_id
            )
            if count > 0:
                blocking.append({"table": table, "label": label, "count": int(count)})

        if blocking:
            raise HTTPException(
                status_code=409,
                detail={
                    "can_delete": False,
                    "suggest_archive": True,
                    "message": "Este ingrediente tiene registros asociados. Puedes archivarlo para ocultarlo sin perder el historial.",
                    "blocking": blocking,
                },
            )

        # Capture resale product IDs BEFORE delete (CASCADE will remove product_recipes rows)
        resale_rows = await conn.fetch(
            """SELECT p.id FROM product p
               JOIN product_recipes pr ON pr.product_id = p.id
               WHERE pr.ingredient_id = $1 AND p.is_resale = true AND p.tenant_id = $2""",
            ingredient_id, tenant_id,
        )
        resale_ids = [str(row["id"]) for row in resale_rows]

        # Deactivate orphaned resale products then delete (CASCADE handles product_recipes)
        if resale_ids:
            await conn.execute(
                "UPDATE product SET is_available = false WHERE id = ANY($1::uuid[]) AND is_resale = true",
                resale_ids,
            )
        await conn.execute("DELETE FROM ingredients WHERE id = $1", ingredient_id)

    return {"success": True, "deleted_id": ingredient_id, "resale_products_deactivated": len(resale_ids)}

"""
Recipe Bases Service - Business logic for recipe base types management
"""
import logging
from typing import Optional
from uuid import UUID
from fastapi import Request, Response, HTTPException
import asyncpg
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError, APIError
from app.models.recipe_base import (
    RecipeBaseTypeCreate,
    RecipeBaseTypeUpdate,
    RecipeBaseType,
    RecipeBaseIngredient,
    RecipeBaseTypeResponse,
    RecipeBaseTypesListResponse
)
from app.services import menu_history_service
from app.services.ingredient_purchase_units_service import resolve_to_base_unit
from app.services.billing_service import check_plan_quota_growth, check_plan_quota_scoped
from app.services.cost_resolution_service import recipe_qty_to_stock_units
from decimal import Decimal

logger = logging.getLogger(__name__)

# Shared SELECT for recipe-base ingredient rows (cost + stock metadata for #704).
_RECIPE_BASE_INGREDIENT_SELECT = """
    SELECT
        brt.id,
        brt.product_base_type_id,
        brt.ingredient_id,
        brt.base_quantity,
        brt.unit,
        brt.is_required,
        brt.notes,
        brt.tenant_id,
        brt.created_at,
        brt.updated_at,
        i.name as ingredient_name,
        COALESCE(i.controla_inventario, false) as controla_inventario,
        i.unit as stock_unit,
        CAST(i.unit_weight_gr AS float) as unit_weight_gr,
        i.unit_weight_unit,
        COALESCE(
            (SELECT pi.unit_cost
             FROM tenant_purchase_items pi
             JOIN tenant_purchases p ON pi.purchase_id = p.id
             WHERE pi.ingredient_id = brt.ingredient_id
             AND p.tenant_id = brt.tenant_id
             AND pi.unit_cost IS NOT NULL
             AND pi.unit_cost > 0
             ORDER BY p.purchase_date DESC
             LIMIT 1),
            i.costo_unitario,
            0
        ) as costo_unitario
    FROM base_recipe_templates brt
    LEFT JOIN ingredients i ON brt.ingredient_id = i.id
    WHERE brt.product_base_type_id = $1 AND brt.tenant_id = $2
    ORDER BY brt.created_at
"""


def _recipe_base_ingredient_from_row(ing_row) -> RecipeBaseIngredient:
    """Build RecipeBaseIngredient with converted costo_linea (#704)."""
    data = dict(ing_row)
    unit_cost = Decimal(str(data.get("costo_unitario") or 0))
    stock_qty = recipe_qty_to_stock_units(
        data.get("base_quantity") or 0,
        data.get("unit"),
        data.get("stock_unit"),
        data.get("unit_weight_gr"),
    )
    data["costo_linea"] = float(stock_qty * unit_cost)
    # Defaults when create path omits optional joins
    data.setdefault("controla_inventario", False)
    return RecipeBaseIngredient(**data)

async def create_recipe_base_type(
    request: Request,
    recipe_data: RecipeBaseTypeCreate
) -> RecipeBaseTypeResponse:
    """
    Create a new recipe base type with its ingredient templates.

    Args:
        request: FastAPI request object (for session/auth)
        recipe_data: Recipe base type data with ingredients

    Returns:
        RecipeBaseTypeResponse with created recipe base type

    Raises:
        AuthenticationError: If tenant_id is missing
        APIError: If database operation fails
    """
    try:
        # Get tenant from session
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        # Validate no duplicate ingredients
        if recipe_data.ingredients:
            ingredient_ids = [ing.ingredient_id for ing in recipe_data.ingredients]
            if len(ingredient_ids) != len(set(ingredient_ids)):
                raise HTTPException(
                    status_code=400,
                    detail="No se puede agregar el mismo ingrediente más de una vez en la misma receta"
                )

        async with get_db_connection() as conn:
            await check_plan_quota_growth(conn, tenant_id, "recipe_bases")
            # Insert product_base_type
            insert_base_query = """
                INSERT INTO product_base_types (name, description, is_active, tenant_id)
                VALUES ($1, $2, $3, $4)
                RETURNING id, name, description, is_active, created_at, updated_at
            """

            base_type_row = await conn.fetchrow(
                insert_base_query,
                recipe_data.name,
                recipe_data.description,
                recipe_data.is_active,
                tenant_id
            )

            base_type_id = base_type_row['id']

            if recipe_data.ingredients:
                await check_plan_quota_scoped(
                    conn,
                    tenant_id,
                    "recipe_base_template_lines",
                    base_type_id,
                    projected_count=len(recipe_data.ingredients),
                )

            # Insert ingredients into base_recipe_templates
            ingredients = []
            if recipe_data.ingredients:
                for ingredient_data in recipe_data.ingredients:
                    insert_ingredient_query = """
                        INSERT INTO base_recipe_templates (
                            product_base_type_id,
                            ingredient_id,
                            base_quantity,
                            unit,
                            is_required,
                            notes,
                            tenant_id
                        )
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        RETURNING id, product_base_type_id, ingredient_id,
                                  base_quantity, unit, is_required, notes,
                                  tenant_id, created_at, updated_at
                    """

                    base_qty, base_unit = await resolve_to_base_unit(
                        conn,
                        ingredient_data.ingredient_id,
                        ingredient_data.base_quantity,
                        ingredient_data.unit
                    )
                    ingredient_row = await conn.fetchrow(
                        insert_ingredient_query,
                        base_type_id,
                        ingredient_data.ingredient_id,
                        base_qty,
                        base_unit,
                        ingredient_data.is_required,
                        ingredient_data.notes,
                        tenant_id
                    )

                    # Fetch ingredient cost/stock metadata for the response
                    meta_query = """
                        SELECT
                            name,
                            unit as stock_unit,
                            CAST(unit_weight_gr AS float) as unit_weight_gr,
                            unit_weight_unit,
                            COALESCE(costo_unitario, 0) as costo_unitario,
                            COALESCE(controla_inventario, false) as controla_inventario
                        FROM ingredients WHERE id = $1
                    """
                    meta_row = await conn.fetchrow(meta_query, ingredient_data.ingredient_id)

                    ingredient_dict = dict(ingredient_row)
                    if meta_row:
                        ingredient_dict['ingredient_name'] = meta_row['name']
                        ingredient_dict['stock_unit'] = meta_row['stock_unit']
                        ingredient_dict['unit_weight_gr'] = meta_row['unit_weight_gr']
                        ingredient_dict['unit_weight_unit'] = meta_row['unit_weight_unit']
                        ingredient_dict['costo_unitario'] = float(meta_row['costo_unitario'] or 0)
                        ingredient_dict['controla_inventario'] = meta_row['controla_inventario']
                    else:
                        ingredient_dict['ingredient_name'] = None

                    ingredients.append(_recipe_base_ingredient_from_row(ingredient_dict))

            # Build response
            recipe_base_type = RecipeBaseType(
                id=base_type_row['id'],
                name=base_type_row['name'],
                description=base_type_row['description'],
                is_active=base_type_row['is_active'],
                created_at=base_type_row['created_at'],
                updated_at=base_type_row['updated_at'],
                ingredients=ingredients
            )

            # Registrar en historial
            user_id = session_context.user_id if hasattr(session_context, 'user_id') else None
            recipe_snapshot = await menu_history_service.get_recipe_base_snapshot(conn, base_type_id, tenant_id)
            if recipe_snapshot:
                await menu_history_service.record_recipe_base_create(
                    conn, tenant_id, base_type_id, recipe_data.name,
                    recipe_snapshot, user_id
                )

            logger.info(f"Created recipe base type: {base_type_id} for tenant: {tenant_id}")

            return RecipeBaseTypeResponse(
                success=True,
                data=recipe_base_type
            )

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=400,
            detail="Ya existe una receta base con ese nombre"
        )
    except Exception as e:
        logger.error(f"Error creating recipe base type: {str(e)}")
        raise APIError(f"Error creating recipe base type: {str(e)}", status_code=500)


async def get_recipe_base_types_list(
    request: Request,
    response: Response,
    page: int = 1,
    limit: int = 50,
    search: Optional[str] = None,
    is_active: Optional[bool] = None,
    include_ingredients: bool = False
) -> RecipeBaseTypesListResponse:
    """
    Get list of recipe base types with optional filtering and pagination.

    Args:
        request: FastAPI request object
        response: FastAPI response object
        page: Page number (1-indexed)
        limit: Items per page
        search: Search term for name
        is_active: Filter by active status
        include_ingredients: Whether to include ingredients in response

    Returns:
        RecipeBaseTypesListResponse with list of recipe base types
    """
    try:
        # Get tenant from session
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection(use_transaction=False) as conn:
            # Build query
            base_query = """
                SELECT id, name, description, is_active, created_at, updated_at
                FROM product_base_types
                WHERE tenant_id = $1
            """

            params = [tenant_id]
            param_count = 2

            # Apply filters
            if search:
                base_query += f" AND LOWER(name) LIKE LOWER(${param_count})"
                params.append(f"%{search}%")
                param_count += 1

            if is_active is not None:
                base_query += f" AND is_active = ${param_count}"
                params.append(is_active)
                param_count += 1

            # Count total
            count_query = f"SELECT COUNT(*) as total FROM ({base_query}) as filtered"
            total_row = await conn.fetchrow(count_query, *params)
            total = total_row['total']

            # Add pagination
            offset = (page - 1) * limit
            base_query += f" ORDER BY created_at DESC LIMIT ${param_count} OFFSET ${param_count + 1}"
            params.extend([limit, offset])

            # Fetch recipe base types
            rows = await conn.fetch(base_query, *params)

            recipe_base_types = []
            for row in rows:
                recipe_dict = dict(row)

                # Optionally fetch ingredients
                ingredients = []
                if include_ingredients:
                    ingredient_rows = await conn.fetch(
                        _RECIPE_BASE_INGREDIENT_SELECT, row['id'], tenant_id
                    )
                    ingredients = [
                        _recipe_base_ingredient_from_row(ing_row)
                        for ing_row in ingredient_rows
                    ]

                recipe_dict['ingredients'] = ingredients
                recipe_base_types.append(RecipeBaseType(**recipe_dict))

            return RecipeBaseTypesListResponse(
                success=True,
                total=total,
                data=recipe_base_types
            )

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching recipe base types: {str(e)}")
        raise APIError(f"Error fetching recipe base types: {str(e)}", status_code=500)


async def get_recipe_base_type_by_id(
    request: Request,
    recipe_base_id: UUID
) -> RecipeBaseTypeResponse:
    """
    Get a single recipe base type by ID with its ingredients.

    Args:
        request: FastAPI request object
        recipe_base_id: UUID of the recipe base type

    Returns:
        RecipeBaseTypeResponse with recipe base type data

    Raises:
        HTTPException: If recipe base type not found
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection(use_transaction=False) as conn:
            # Fetch recipe base type
            base_query = """
                SELECT id, name, description, is_active, created_at, updated_at
                FROM product_base_types
                WHERE id = $1 AND tenant_id = $2
            """

            row = await conn.fetchrow(base_query, recipe_base_id, tenant_id)

            if not row:
                raise HTTPException(status_code=404, detail="Recipe base type not found")

            # Fetch ingredients
            ingredient_rows = await conn.fetch(
                _RECIPE_BASE_INGREDIENT_SELECT, recipe_base_id, tenant_id
            )
            ingredients = [
                _recipe_base_ingredient_from_row(ing_row)
                for ing_row in ingredient_rows
            ]

            recipe_dict = dict(row)
            recipe_dict['ingredients'] = ingredients

            return RecipeBaseTypeResponse(
                success=True,
                data=RecipeBaseType(**recipe_dict)
            )

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching recipe base type {recipe_base_id}: {str(e)}")
        raise APIError(f"Error fetching recipe base type: {str(e)}", status_code=500)


async def update_recipe_base_type(
    request: Request,
    recipe_base_id: UUID,
    update_data: RecipeBaseTypeUpdate
) -> RecipeBaseTypeResponse:
    """
    Update a recipe base type and optionally its ingredients.

    This endpoint updates the base type info (name, description, is_active) and
    can also update the ingredients list. If ingredients are provided, all existing
    ingredients will be replaced with the new list.

    Args:
        request: FastAPI request object
        recipe_base_id: UUID of the recipe base type
        update_data: Fields to update (including optional ingredients list)

    Returns:
        RecipeBaseTypeResponse with updated recipe base type
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        # Build dynamic update query
        update_fields = []
        params = []
        param_count = 1

        if update_data.name is not None:
            update_fields.append(f"name = ${param_count}")
            params.append(update_data.name)
            param_count += 1

        if update_data.description is not None:
            update_fields.append(f"description = ${param_count}")
            params.append(update_data.description)
            param_count += 1

        if update_data.is_active is not None:
            update_fields.append(f"is_active = ${param_count}")
            params.append(update_data.is_active)
            param_count += 1

        async with get_db_connection() as conn:
            # Obtener snapshot ANTES de actualizar (para historial)
            old_snapshot = await menu_history_service.get_recipe_base_snapshot(conn, recipe_base_id, tenant_id)
            user_id = session_context.user_id if hasattr(session_context, 'user_id') else None

            # Update base type fields if provided
            if update_fields:
                update_fields.append(f"updated_at = NOW()")
                params.append(recipe_base_id)
                params.append(tenant_id)

                update_query = f"""
                    UPDATE product_base_types
                    SET {', '.join(update_fields)}
                    WHERE id = ${param_count} AND tenant_id = ${param_count + 1}
                    RETURNING id, name, description, is_active, created_at, updated_at
                """

                row = await conn.fetchrow(update_query, *params)

                if not row:
                    raise HTTPException(status_code=404, detail="Recipe base type not found")
            else:
                # If no fields to update, just fetch the current data
                fetch_query = """
                    SELECT id, name, description, is_active, created_at, updated_at
                    FROM product_base_types
                    WHERE id = $1 AND tenant_id = $2
                """
                row = await conn.fetchrow(fetch_query, recipe_base_id, tenant_id)

                if not row:
                    raise HTTPException(status_code=404, detail="Recipe base type not found")

            # Update ingredients if provided
            if update_data.ingredients is not None:
                # Validate no duplicate ingredients
                ingredient_ids = [ing.ingredient_id for ing in update_data.ingredients]
                if len(ingredient_ids) != len(set(ingredient_ids)):
                    raise HTTPException(
                        status_code=400,
                        detail="No se puede agregar el mismo ingrediente más de una vez en la misma receta"
                    )

                # Delete existing ingredients
                delete_ingredients_query = """
                    DELETE FROM base_recipe_templates
                    WHERE product_base_type_id = $1 AND tenant_id = $2
                """
                await conn.execute(delete_ingredients_query, recipe_base_id, tenant_id)

                if update_data.ingredients:
                    await check_plan_quota_scoped(
                        conn,
                        tenant_id,
                        "recipe_base_template_lines",
                        recipe_base_id,
                        projected_count=len(update_data.ingredients),
                    )

                # Insert new ingredients
                for ingredient_data in update_data.ingredients:
                    insert_ingredient_query = """
                        INSERT INTO base_recipe_templates (
                            product_base_type_id,
                            ingredient_id,
                            base_quantity,
                            unit,
                            is_required,
                            notes,
                            tenant_id
                        )
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """
                    base_qty, base_unit = await resolve_to_base_unit(
                        conn,
                        ingredient_data.ingredient_id,
                        ingredient_data.base_quantity,
                        ingredient_data.unit
                    )
                    if base_qty >= 1_000_000_000_000:
                        raise APIError(
                            "La cantidad convertida es demasiado grande. Verifica la unidad y la cantidad ingresada.",
                            status_code=422,
                        )
                    await conn.execute(
                        insert_ingredient_query,
                        recipe_base_id,
                        ingredient_data.ingredient_id,
                        base_qty,
                        base_unit,
                        ingredient_data.is_required,
                        ingredient_data.notes,
                        tenant_id
                    )

            # Fetch ingredients
            ingredient_rows = await conn.fetch(
                _RECIPE_BASE_INGREDIENT_SELECT, recipe_base_id, tenant_id
            )
            ingredients = [
                _recipe_base_ingredient_from_row(ing_row)
                for ing_row in ingredient_rows
            ]

            recipe_dict = dict(row)
            recipe_dict['ingredients'] = ingredients

            # Registrar cambios en historial
            if old_snapshot:
                new_snapshot = await menu_history_service.get_recipe_base_snapshot(conn, recipe_base_id, tenant_id)
                recipe_name = old_snapshot.get('name', row['name'])
                if new_snapshot:
                    await menu_history_service.compare_and_record_recipe_base_changes(
                        conn, tenant_id, recipe_base_id, recipe_name,
                        old_snapshot, new_snapshot, user_id
                    )

            logger.info(f"Updated recipe base type: {recipe_base_id}")

            return RecipeBaseTypeResponse(
                success=True,
                data=RecipeBaseType(**recipe_dict)
            )

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=400,
            detail="Ya existe una receta base con ese nombre"
        )
    except Exception as e:
        logger.error(f"Error updating recipe base type {recipe_base_id}: {str(e)}")
        raise APIError(f"Error updating recipe base type: {str(e)}", status_code=500)


async def delete_recipe_base_type(
    request: Request,
    recipe_base_id: UUID
) -> dict:
    """
    Delete a recipe base type and its ingredient templates.

    Args:
        request: FastAPI request object
        recipe_base_id: UUID of the recipe base type to delete

    Returns:
        Success message

    Raises:
        HTTPException: If recipe base type not found
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Check if exists
            check_query = """
                SELECT id, name FROM product_base_types WHERE id = $1 AND tenant_id = $2
            """
            exists = await conn.fetchrow(check_query, recipe_base_id, tenant_id)

            if not exists:
                raise HTTPException(status_code=404, detail="Recipe base type not found")

            # Obtener snapshot ANTES de eliminar (para historial)
            recipe_snapshot = await menu_history_service.get_recipe_base_snapshot(conn, recipe_base_id, tenant_id)
            recipe_name = exists['name']
            user_id = session_context.user_id if hasattr(session_context, 'user_id') else None

            # Registrar eliminación en historial
            if recipe_snapshot:
                await menu_history_service.record_recipe_base_delete(
                    conn, tenant_id, recipe_base_id, recipe_name,
                    recipe_snapshot, user_id
                )

            # Delete ingredients (cascade should handle this, but being explicit)
            delete_ingredients_query = """
                DELETE FROM base_recipe_templates
                WHERE product_base_type_id = $1 AND tenant_id = $2
            """
            await conn.execute(delete_ingredients_query, recipe_base_id, tenant_id)

            # Delete recipe base type
            delete_query = """
                DELETE FROM product_base_types
                WHERE id = $1 AND tenant_id = $2
            """
            await conn.execute(delete_query, recipe_base_id, tenant_id)

            logger.info(f"Deleted recipe base type: {recipe_base_id}")

            return {
                "success": True,
                "message": "Recipe base type deleted successfully"
            }

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting recipe base type {recipe_base_id}: {str(e)}")
        raise APIError(f"Error deleting recipe base type: {str(e)}", status_code=500)

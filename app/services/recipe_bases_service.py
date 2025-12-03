"""
Recipe Bases Service - Business logic for recipe base types management
"""
import logging
from typing import Optional
from uuid import UUID
from fastapi import Request, Response, HTTPException
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

logger = logging.getLogger(__name__)


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
            # Insert product_base_type
            insert_base_query = """
                INSERT INTO product_base_types (name, description, is_active)
                VALUES ($1, $2, $3)
                RETURNING id, name, description, is_active, created_at, updated_at
            """

            base_type_row = await conn.fetchrow(
                insert_base_query,
                recipe_data.name,
                recipe_data.description,
                recipe_data.is_active
            )

            base_type_id = base_type_row['id']

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

                    ingredient_row = await conn.fetchrow(
                        insert_ingredient_query,
                        base_type_id,
                        ingredient_data.ingredient_id,
                        ingredient_data.base_quantity,
                        ingredient_data.unit,
                        ingredient_data.is_required,
                        ingredient_data.notes,
                        tenant_id
                    )

                    # Fetch ingredient name for the response
                    name_query = """
                        SELECT name FROM ingredients WHERE id = $1
                    """
                    ingredient_name_row = await conn.fetchrow(name_query, ingredient_data.ingredient_id)

                    ingredient_dict = dict(ingredient_row)
                    ingredient_dict['ingredient_name'] = ingredient_name_row['name'] if ingredient_name_row else None

                    ingredients.append(RecipeBaseIngredient(**ingredient_dict))

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

            logger.info(f"Created recipe base type: {base_type_id} for tenant: {tenant_id}")

            return RecipeBaseTypeResponse(
                success=True,
                data=recipe_base_type
            )

    except AuthenticationError:
        raise
    except HTTPException:
        raise
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
                WHERE 1=1
            """

            params = []
            param_count = 1

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
                    ingredients_query = """
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
                            i.name as ingredient_name
                        FROM base_recipe_templates brt
                        LEFT JOIN ingredients i ON brt.ingredient_id = i.id
                        WHERE brt.product_base_type_id = $1
                        ORDER BY brt.created_at
                    """

                    ingredient_rows = await conn.fetch(ingredients_query, row['id'])
                    ingredients = [RecipeBaseIngredient(**dict(ing_row)) for ing_row in ingredient_rows]

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
                WHERE id = $1
            """

            row = await conn.fetchrow(base_query, recipe_base_id)

            if not row:
                raise HTTPException(status_code=404, detail="Recipe base type not found")

            # Fetch ingredients
            ingredients_query = """
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
                    i.name as ingredient_name
                FROM base_recipe_templates brt
                LEFT JOIN ingredients i ON brt.ingredient_id = i.id
                WHERE brt.product_base_type_id = $1
                ORDER BY brt.created_at
            """

            ingredient_rows = await conn.fetch(ingredients_query, recipe_base_id)
            ingredients = [RecipeBaseIngredient(**dict(ing_row)) for ing_row in ingredient_rows]

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
            # Update base type fields if provided
            if update_fields:
                update_fields.append(f"updated_at = NOW()")
                params.append(recipe_base_id)

                update_query = f"""
                    UPDATE product_base_types
                    SET {', '.join(update_fields)}
                    WHERE id = ${param_count}
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
                    WHERE id = $1
                """
                row = await conn.fetchrow(fetch_query, recipe_base_id)

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
                    WHERE product_base_type_id = $1
                """
                await conn.execute(delete_ingredients_query, recipe_base_id)

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
                    await conn.execute(
                        insert_ingredient_query,
                        recipe_base_id,
                        ingredient_data.ingredient_id,
                        ingredient_data.base_quantity,
                        ingredient_data.unit,
                        ingredient_data.is_required,
                        ingredient_data.notes,
                        tenant_id
                    )

            # Fetch ingredients
            ingredients_query = """
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
                    i.name as ingredient_name
                FROM base_recipe_templates brt
                LEFT JOIN ingredients i ON brt.ingredient_id = i.id
                WHERE brt.product_base_type_id = $1
                ORDER BY brt.created_at
            """

            ingredient_rows = await conn.fetch(ingredients_query, recipe_base_id)
            ingredients = [RecipeBaseIngredient(**dict(ing_row)) for ing_row in ingredient_rows]

            recipe_dict = dict(row)
            recipe_dict['ingredients'] = ingredients

            logger.info(f"Updated recipe base type: {recipe_base_id}")

            return RecipeBaseTypeResponse(
                success=True,
                data=RecipeBaseType(**recipe_dict)
            )

    except AuthenticationError:
        raise
    except HTTPException:
        raise
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
                SELECT id FROM product_base_types WHERE id = $1
            """
            exists = await conn.fetchrow(check_query, recipe_base_id)

            if not exists:
                raise HTTPException(status_code=404, detail="Recipe base type not found")

            # Delete ingredients (cascade should handle this, but being explicit)
            delete_ingredients_query = """
                DELETE FROM base_recipe_templates
                WHERE product_base_type_id = $1
            """
            await conn.execute(delete_ingredients_query, recipe_base_id)

            # Delete recipe base type
            delete_query = """
                DELETE FROM product_base_types
                WHERE id = $1
            """
            await conn.execute(delete_query, recipe_base_id)

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

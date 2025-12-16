from typing import List, Optional
from uuid import UUID
from fastapi import Request, Response, HTTPException
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError, APIError
from app.models.product import (
    Product, ProductCreate, ProductUpdate, ProductsListResponse,
    ProductResponse, ProductStats, RecipeIngredient
)
from decimal import Decimal
import logging

logger = logging.getLogger(__name__)

async def create_product_with_recipe(
    request: Request,
    product_data: ProductCreate
) -> ProductResponse:
    """
    Creates a product with its recipe in a single transaction.
    Steps:
    1. Insert product
    2. Insert recipe ingredients
    3. Calculate and update product cost
    4. Return complete product with recipe
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id or product_data.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        # VALIDATION: All products MUST have at least one ingredient since inventory is always controlled
        if not product_data.ingredients or len(product_data.ingredients) == 0:
            raise HTTPException(
                status_code=400,
                detail="El producto debe tener al menos un ingrediente. Todos los productos controlan inventario automáticamente."
            )

        async with get_db_connection() as conn:
            # Start transaction
            async with conn.transaction():
                # 1. Insert product
                # NOTE: controla_stock is ALWAYS True - all products control inventory
                product_query = """
                    INSERT INTO product (
                        name, description, price, category_id, product_base_type_id, preparation_time,
                        controla_stock, is_available, is_combo, allow_modifiers, tenant_id
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                    RETURNING id, created_at, updated_at
                """
                product_result = await conn.fetchrow(
                    product_query,
                    product_data.name,
                    product_data.description,
                    product_data.price,
                    product_data.category_id,
                    product_data.product_base_type_id,
                    product_data.preparation_time,
                    True,  # ALWAYS True - controla_stock is mandatory
                    product_data.is_available,
                    product_data.is_combo,
                    product_data.allow_modifiers,
                    tenant_id
                )

                product_id = product_result['id']

                # 2. Insert recipe base associations
                if product_data.recipe_base_ids:
                    # Remove duplicates
                    unique_recipe_base_ids = list(set(product_data.recipe_base_ids))

                    base_recipe_query = """
                        INSERT INTO product_base_recipes (
                            product_id, product_base_type_id, tenant_id
                        )
                        VALUES ($1, $2, $3)
                    """
                    for recipe_base_id in unique_recipe_base_ids:
                        await conn.execute(
                            base_recipe_query,
                            product_id,
                            recipe_base_id,
                            tenant_id
                        )

                # 3. Insert recipe ingredients
                if product_data.ingredients:
                    recipe_query = """
                        INSERT INTO product_recipes (
                            product_id, ingredient_id, quantity, unit, tenant_id
                        )
                        VALUES ($1, $2, $3, $4, $5)
                    """

                    for ingredient in product_data.ingredients:
                        await conn.execute(
                            recipe_query,
                            product_id,
                            ingredient.ingredient_id,
                            ingredient.quantity,
                            ingredient.unit,
                            tenant_id
                        )

                # 4. Calculate and update product cost
                cost_query = """
                    UPDATE product
                    SET costo_calculado = (
                        SELECT COALESCE(SUM(pr.quantity * i.costo_unitario), 0)
                        FROM product_recipes pr
                        JOIN ingredients i ON pr.ingredient_id = i.id
                        WHERE pr.product_id = $1
                    )
                    WHERE id = $1
                """
                await conn.execute(cost_query, product_id)

                # 4. Get complete product with recipe
                return await get_product_by_id(request, product_id, conn)

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error creating product: {str(e)}")
        raise APIError(f"Error creating product: {str(e)}", status_code=500)


async def get_product_by_id(
    request: Request,
    product_id: UUID,
    conn=None
) -> ProductResponse:
    """Get a single product with its recipe and calculated fields"""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        # Use provided connection or create new one
        async def _fetch_product(connection):
            # Get product with category
            product_query = """
                SELECT
                    p.id,
                    p.name,
                    p.description,
                    p.price,
                    p.category_id,
                    c.name as category_name,
                    p.preparation_time,
                    p.controla_stock,
                    p.is_available,
                    p.is_combo,
                    p.allow_modifiers,
                    p.costo_calculado,
                    p.precio_sugerido,
                    p.margen_objetivo,
                    p.tenant_id,
                    p.created_at,
                    p.updated_at,
                    -- Calculated fields
                    CASE
                        WHEN p.costo_calculado > 0
                        THEN ((p.price - p.costo_calculado) / p.costo_calculado * 100)
                        ELSE NULL
                    END as margen_porcentaje,
                    (p.price - COALESCE(p.costo_calculado, 0)) as margen_valor
                FROM product p
                LEFT JOIN categories c ON p.category_id = c.id
                WHERE p.id = $1 AND p.tenant_id = $2
            """

            product_row = await connection.fetchrow(product_query, product_id, tenant_id)

            if not product_row:
                raise HTTPException(status_code=404, detail="Product not found")

            # Get recipe ingredients
            recipe_query = """
                SELECT
                    pr.id,
                    pr.product_id,
                    pr.ingredient_id,
                    pr.quantity,
                    pr.unit,
                    i.name as ingredient_name,
                    i.costo_unitario as ingredient_cost_per_unit,
                    (pr.quantity * i.costo_unitario) as total_cost
                FROM product_recipes pr
                JOIN ingredients i ON pr.ingredient_id = i.id
                WHERE pr.product_id = $1
                ORDER BY i.name
            """

            recipe_rows = await connection.fetch(recipe_query, product_id)

            # Get recipe base IDs
            recipe_base_query = """
                SELECT product_base_type_id
                FROM product_base_recipes
                WHERE product_id = $1
                ORDER BY created_at
            """
            recipe_base_rows = await connection.fetch(recipe_base_query, product_id)

            # Get modifier groups with modifiers
            modifier_groups_query = """
                SELECT
                    mg.id,
                    mg.name,
                    mg.min_qty,
                    mg.max_qty,
                    mg.is_required,
                    mg.sort_order
                FROM modifier_groups mg
                WHERE mg.product_id = $1
                ORDER BY mg.sort_order, mg.name
            """
            modifier_groups_rows = await connection.fetch(modifier_groups_query, product_id)

            # Get all modifiers in a single query
            if modifier_groups_rows:
                group_ids = [row['id'] for row in modifier_groups_rows]
                modifiers_query = """
                    SELECT
                        m.id,
                        m.modifier_group_id,
                        m.name,
                        m.price,
                        m.is_available,
                        m.is_default,
                        m.sort_order
                    FROM modifiers m
                    WHERE m.modifier_group_id = ANY($1::uuid[])
                    ORDER BY m.sort_order, m.name
                """
                modifiers_rows = await connection.fetch(modifiers_query, group_ids)

                # Group modifiers by modifier_group_id
                modifiers_by_group = {}
                for mod in modifiers_rows:
                    group_id = mod['modifier_group_id']
                    if group_id not in modifiers_by_group:
                        modifiers_by_group[group_id] = []
                    modifiers_by_group[group_id].append({
                        'id': mod['id'],
                        'name': mod['name'],
                        'price': mod['price'],
                        'is_available': mod['is_available'],
                        'is_default': mod['is_default'],
                        'sort_order': mod['sort_order']
                    })

                # Build modifier groups with their modifiers
                modifier_groups = []
                for mg_row in modifier_groups_rows:
                    group_dict = dict(mg_row)
                    group_dict['modifiers'] = modifiers_by_group.get(mg_row['id'], [])
                    modifier_groups.append(group_dict)
            else:
                modifier_groups = []

            # Build product dict
            product_dict = dict(product_row)
            product_dict['ingredients'] = [dict(row) for row in recipe_rows]
            product_dict['recipe_base_ids'] = [row['product_base_type_id'] for row in recipe_base_rows]
            product_dict['modifier_groups'] = modifier_groups

            return ProductResponse(data=Product(**product_dict))

        if conn:
            return await _fetch_product(conn)
        else:
            async with get_db_connection() as connection:
                return await _fetch_product(connection)

    except HTTPException:
        raise
    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error fetching product: {str(e)}")
        raise APIError(f"Error fetching product: {str(e)}", status_code=500)


async def get_products_list(
    request: Request,
    response: Response,
    page: int = 1,
    limit: int = 50,
    search: Optional[str] = None,
    category_id: Optional[UUID] = None,
    is_available: Optional[bool] = None,
    is_combo: Optional[bool] = None,
    include_ingredients: bool = False
) -> ProductsListResponse:
    """Get list of products with filters"""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Base query
            base_query = """
                SELECT
                    p.id,
                    p.name,
                    p.description,
                    p.price,
                    p.category_id,
                    c.name as category_name,
                    p.preparation_time,
                    p.controla_stock,
                    p.is_available,
                    p.is_combo,
                    p.allow_modifiers,
                    p.costo_calculado,
                    p.precio_sugerido,
                    p.margen_objetivo,
                    p.tenant_id,
                    p.created_at,
                    p.updated_at,
                    CASE
                        WHEN p.costo_calculado > 0
                        THEN ((p.price - p.costo_calculado) / p.costo_calculado * 100)
                        ELSE NULL
                    END as margen_porcentaje,
                    (p.price - COALESCE(p.costo_calculado, 0)) as margen_valor
                FROM product p
                LEFT JOIN categories c ON p.category_id = c.id
                WHERE p.tenant_id = $1
            """

            count_query = "SELECT COUNT(*) FROM product WHERE tenant_id = $1"

            params = [tenant_id]
            param_count = 2

            # Add filters
            if search:
                base_query += f" AND (LOWER(p.name) LIKE LOWER(${param_count}) OR LOWER(p.description) LIKE LOWER(${param_count}))"
                count_query += f" AND (LOWER(name) LIKE LOWER(${param_count}) OR LOWER(description) LIKE LOWER(${param_count}))"
                params.append(f"%{search}%")
                param_count += 1

            if category_id:
                base_query += f" AND p.category_id = ${param_count}"
                count_query += f" AND category_id = ${param_count}"
                params.append(category_id)
                param_count += 1

            if is_available is not None:
                base_query += f" AND p.is_available = ${param_count}"
                count_query += f" AND is_available = ${param_count}"
                params.append(is_available)
                param_count += 1

            if is_combo is not None:
                base_query += f" AND p.is_combo = ${param_count}"
                count_query += f" AND is_combo = ${param_count}"
                params.append(is_combo)
                param_count += 1

            # Add pagination
            offset = (page - 1) * limit
            base_query += f" ORDER BY p.created_at DESC LIMIT ${param_count} OFFSET ${param_count + 1}"

            # Execute queries
            products_data = await conn.fetch(base_query, *params, limit, offset)
            count_result = await conn.fetchrow(count_query, *params)

            total = count_result['count']

            # Convert to Product models
            products = []
            for row in products_data:
                product_dict = dict(row)

                # Fetch ingredients if requested
                if include_ingredients:
                    recipe_query = """
                        SELECT
                            pr.id,
                            pr.product_id,
                            pr.ingredient_id,
                            pr.quantity,
                            pr.unit,
                            i.name as ingredient_name,
                            i.costo_unitario as ingredient_cost_per_unit,
                            (pr.quantity * i.costo_unitario) as total_cost
                        FROM product_recipes pr
                        JOIN ingredients i ON pr.ingredient_id = i.id
                        WHERE pr.product_id = $1
                        ORDER BY i.name
                    """
                    recipe_rows = await conn.fetch(recipe_query, row['id'])
                    product_dict['ingredients'] = [dict(r) for r in recipe_rows]
                else:
                    product_dict['ingredients'] = []  # Empty for list view

                products.append(Product(**product_dict))

            return ProductsListResponse(
                success=True,
                total=total,
                data=products
            )

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error fetching products: {str(e)}")
        raise APIError(f"Error fetching products: {str(e)}", status_code=500)


async def get_product_stats(request: Request) -> ProductStats:
    """Get product statistics for dashboard"""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            stats_query = """
                SELECT
                    COUNT(*) as total,
                    COUNT(*) FILTER (WHERE is_available = TRUE) as available,
                    COUNT(*) FILTER (WHERE controla_stock = TRUE) as with_stock_control,
                    COUNT(*) FILTER (WHERE is_combo = TRUE) as combos,
                    AVG(
                        CASE
                            WHEN costo_calculado > 0
                            THEN ((price - costo_calculado) / costo_calculado * 100)
                            ELSE NULL
                        END
                    ) as avg_margin
                FROM product
                WHERE tenant_id = $1
            """

            stats_row = await conn.fetchrow(stats_query, tenant_id)

            return ProductStats(**dict(stats_row))

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error fetching product stats: {str(e)}")
        raise APIError(f"Error fetching product stats: {str(e)}", status_code=500)


async def update_product_with_recipe(
    request: Request,
    product_id: UUID,
    product_data: ProductUpdate
) -> ProductResponse:
    """
    Updates a product with its recipe in a single transaction.
    Steps:
    1. Update product fields
    2. If ingredients provided, replace recipe (delete old, insert new)
    3. Recalculate and update product cost
    4. Return complete product with recipe
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Verify product exists and belongs to tenant
            verify_query = "SELECT id FROM product WHERE id = $1 AND tenant_id = $2"
            product_exists = await conn.fetchrow(verify_query, product_id, tenant_id)

            if not product_exists:
                raise HTTPException(status_code=404, detail="Product not found")

            # Start transaction
            async with conn.transaction():
                # 1. Build update query dynamically based on provided fields
                # NOTE: controla_stock is ALWAYS excluded from updates - it's always True
                update_fields = []
                update_values = []
                param_count = 1

                for field, value in product_data.dict(exclude={'ingredients', 'recipe_base_ids', 'controla_stock'}, exclude_unset=True).items():
                    if value is not None:
                        update_fields.append(f"{field} = ${param_count}")
                        update_values.append(value)
                        param_count += 1

                # Force controla_stock to always be True
                update_fields.append(f"controla_stock = TRUE")

                if update_fields:
                    update_fields.append(f"updated_at = NOW()")
                    update_query = f"""
                        UPDATE product
                        SET {', '.join(update_fields)}
                        WHERE id = ${param_count} AND tenant_id = ${param_count + 1}
                    """
                    update_values.extend([product_id, tenant_id])
                    await conn.execute(update_query, *update_values)

                # 2. Update recipe base associations if provided
                if product_data.recipe_base_ids is not None:
                    # Delete existing associations
                    delete_base_recipe_query = "DELETE FROM product_base_recipes WHERE product_id = $1"
                    await conn.execute(delete_base_recipe_query, product_id)

                    # Insert new associations
                    if product_data.recipe_base_ids:
                        # Remove duplicates
                        unique_recipe_base_ids = list(set(product_data.recipe_base_ids))

                        base_recipe_query = """
                            INSERT INTO product_base_recipes (
                                product_id, product_base_type_id, tenant_id
                            )
                            VALUES ($1, $2, $3)
                        """
                        for recipe_base_id in unique_recipe_base_ids:
                            await conn.execute(
                                base_recipe_query,
                                product_id,
                                recipe_base_id,
                                tenant_id
                            )

                # 3. Update recipe if ingredients provided
                if product_data.ingredients is not None:
                    # VALIDATION: Cannot remove all ingredients - products must have at least one
                    if len(product_data.ingredients) == 0:
                        raise HTTPException(
                            status_code=400,
                            detail="El producto debe tener al menos un ingrediente. Todos los productos controlan inventario automáticamente."
                        )

                    # Delete existing recipe
                    delete_recipe_query = "DELETE FROM product_recipes WHERE product_id = $1"
                    await conn.execute(delete_recipe_query, product_id)

                    # Insert new recipe
                    if product_data.ingredients:
                        recipe_query = """
                            INSERT INTO product_recipes (
                                product_id, ingredient_id, quantity, unit, tenant_id
                            )
                            VALUES ($1, $2, $3, $4, $5)
                        """

                        for ingredient in product_data.ingredients:
                            await conn.execute(
                                recipe_query,
                                product_id,
                                ingredient.ingredient_id,
                                ingredient.quantity,
                                ingredient.unit,
                                tenant_id
                            )

                    # 4. Recalculate product cost
                    cost_query = """
                        UPDATE product
                        SET costo_calculado = (
                            SELECT COALESCE(SUM(pr.quantity * i.costo_unitario), 0)
                            FROM product_recipes pr
                            JOIN ingredients i ON pr.ingredient_id = i.id
                            WHERE pr.product_id = $1
                        )
                        WHERE id = $1
                    """
                    await conn.execute(cost_query, product_id)

                # 4. Get complete updated product
                return await get_product_by_id(request, product_id, conn)

    except HTTPException:
        raise
    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error updating product: {str(e)}")
        raise APIError(f"Error updating product: {str(e)}", status_code=500)


async def delete_product(
    request: Request,
    product_id: UUID
) -> dict:
    """
    Deletes a product and its recipe.
    Returns success status.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Verify product exists and belongs to tenant
            verify_query = "SELECT id FROM product WHERE id = $1 AND tenant_id = $2"
            product_exists = await conn.fetchrow(verify_query, product_id, tenant_id)

            if not product_exists:
                raise HTTPException(status_code=404, detail="Product not found")

            # Start transaction
            async with conn.transaction():
                # Delete recipe first (foreign key constraint)
                delete_recipe_query = "DELETE FROM product_recipes WHERE product_id = $1"
                await conn.execute(delete_recipe_query, product_id)

                # Delete product
                delete_product_query = "DELETE FROM product WHERE id = $1 AND tenant_id = $2"
                await conn.execute(delete_product_query, product_id, tenant_id)

                return {
                    "success": True,
                    "message": "Product deleted successfully"
                }

    except HTTPException:
        raise
    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error deleting product: {str(e)}")
        raise APIError(f"Error deleting product: {str(e)}", status_code=500)

from typing import List, Optional
from uuid import UUID
from fastapi import Request, Response, HTTPException
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError, APIError
from app.models.modifier import (
    ModifierGroup, ModifierGroupCreate, ModifierGroupUpdate,
    ModifierGroupsListResponse, ModifierGroupResponse, ModifierGroupStats,
    Modifier, ProductInfo, IngredientInfo
)
import logging

logger = logging.getLogger(__name__)

async def create_modifier_group(
    request: Request,
    group_data: ModifierGroupCreate
) -> ModifierGroupResponse:
    """
    Creates a modifier group with its modifiers and product associations in a single transaction.
    Now supports associating with multiple products via junction table.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id or group_data.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                # 1. Insert modifier group (without product_id)
                group_query = """
                    INSERT INTO modifier_groups (
                        tenant_id, name, min_qty, max_qty,
                        is_required, sort_order
                    )
                    VALUES ($1, $2, $3, $4, $5, $6)
                    RETURNING id, created_at, updated_at
                """
                group_result = await conn.fetchrow(
                    group_query,
                    tenant_id,
                    group_data.name,
                    group_data.min_qty,
                    group_data.max_qty,
                    group_data.is_required,
                    group_data.sort_order
                )

                group_id = group_result['id']

                # 2. Insert product associations in junction table
                product_assoc_query = """
                    INSERT INTO product_modifier_groups (
                        product_id, modifier_group_id, tenant_id
                    )
                    VALUES ($1, $2, $3)
                """
                for product_id in group_data.product_ids:
                    await conn.execute(
                        product_assoc_query,
                        product_id,
                        group_id,
                        tenant_id
                    )

                # 3. Insert modifiers (with ingredient linking)
                if group_data.modifiers:
                    modifier_query = """
                        INSERT INTO modifiers (
                            modifier_group_id, name, price, max_limit,
                            is_default, is_available, sort_order,
                            ingredient_id, ingredient_quantity, ingredient_unit
                        )
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    """

                    for modifier in group_data.modifiers:
                        await conn.execute(
                            modifier_query,
                            group_id,
                            modifier.name,
                            modifier.price,
                            modifier.max_limit,
                            modifier.is_default,
                            modifier.is_available,
                            modifier.sort_order,
                            modifier.ingredient_id,
                            modifier.ingredient_quantity,
                            modifier.ingredient_unit
                        )

                # 4. Get complete group with modifiers and products
                return await get_modifier_group_by_id(request, group_id, conn)

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error creating modifier group: {str(e)}")
        raise APIError(f"Error creating modifier group: {str(e)}", status_code=500)


async def get_modifier_group_by_id(
    request: Request,
    group_id: UUID,
    conn=None
) -> ModifierGroupResponse:
    """Get a single modifier group with its modifiers and associated products"""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async def _fetch_group(connection):
            # Get group (without product_id - now using junction table)
            group_query = """
                SELECT
                    mg.id,
                    mg.tenant_id,
                    mg.name,
                    mg.min_qty,
                    mg.max_qty,
                    mg.is_required,
                    mg.sort_order,
                    mg.created_at,
                    mg.updated_at
                FROM modifier_groups mg
                WHERE mg.id = $1 AND mg.tenant_id = $2
            """

            group_row = await connection.fetchrow(group_query, group_id, tenant_id)

            if not group_row:
                raise HTTPException(status_code=404, detail="Modifier group not found")

            # Get associated products from junction table
            products_query = """
                SELECT p.id, p.name
                FROM product_modifier_groups pmg
                JOIN product p ON pmg.product_id = p.id
                WHERE pmg.modifier_group_id = $1
                ORDER BY p.name
            """
            products_rows = await connection.fetch(products_query, group_id)

            # Get modifiers with ingredient info
            modifiers_query = """
                SELECT
                    m.id,
                    m.modifier_group_id,
                    m.name,
                    m.price,
                    m.max_limit,
                    m.is_default,
                    m.is_available,
                    m.sort_order,
                    m.created_at,
                    m.updated_at,
                    m.ingredient_id,
                    m.ingredient_quantity,
                    m.ingredient_unit,
                    i.name as ingredient_name,
                    i.unit as ingredient_base_unit,
                    i.costo_unitario,
                    i.controla_inventario
                FROM modifiers m
                LEFT JOIN ingredients i ON m.ingredient_id = i.id
                WHERE m.modifier_group_id = $1
                ORDER BY m.sort_order, m.name
            """

            modifier_rows = await connection.fetch(modifiers_query, group_id)

            # Build group dict
            group_dict = dict(group_row)
            group_dict['products'] = [ProductInfo(id=row['id'], name=row['name']) for row in products_rows]

            # Build modifiers with ingredient info
            modifiers = []
            for row in modifier_rows:
                mod_dict = {
                    'id': row['id'],
                    'modifier_group_id': row['modifier_group_id'],
                    'name': row['name'],
                    'price': row['price'],
                    'max_limit': row['max_limit'],
                    'is_default': row['is_default'],
                    'is_available': row['is_available'],
                    'sort_order': row['sort_order'],
                    'created_at': row['created_at'],
                    'updated_at': row['updated_at'],
                    'ingredient_id': row['ingredient_id'],
                    'ingredient_quantity': row['ingredient_quantity'],
                    'ingredient_unit': row['ingredient_unit'],
                }
                # Add ingredient info if linked
                if row['ingredient_id']:
                    mod_dict['ingredient'] = IngredientInfo(
                        id=row['ingredient_id'],
                        name=row['ingredient_name'],
                        unit=row['ingredient_base_unit'],
                        costo_unitario=row['costo_unitario'],
                        controla_inventario=row['controla_inventario'] or False
                    )
                modifiers.append(Modifier(**mod_dict))

            group_dict['modifiers'] = modifiers

            return ModifierGroupResponse(data=ModifierGroup(**group_dict))

        if conn:
            return await _fetch_group(conn)
        else:
            async with get_db_connection() as connection:
                return await _fetch_group(connection)

    except HTTPException:
        raise
    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error fetching modifier group: {str(e)}")
        raise APIError(f"Error fetching modifier group: {str(e)}", status_code=500)


async def get_modifier_groups_list(
    request: Request,
    response: Response,
    page: int = 1,
    limit: int = 50,
    search: Optional[str] = None,
    product_id: Optional[UUID] = None
) -> ModifierGroupsListResponse:
    """Get list of modifier groups with filters. Now supports multiple products per group."""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Base query - now using junction table for product filtering
            base_query = """
                SELECT DISTINCT
                    mg.id,
                    mg.tenant_id,
                    mg.name,
                    mg.min_qty,
                    mg.max_qty,
                    mg.is_required,
                    mg.sort_order,
                    mg.created_at,
                    mg.updated_at
                FROM modifier_groups mg
                LEFT JOIN product_modifier_groups pmg ON mg.id = pmg.modifier_group_id
                LEFT JOIN product p ON pmg.product_id = p.id
                WHERE mg.tenant_id = $1
            """

            count_query = """
                SELECT COUNT(DISTINCT mg.id)
                FROM modifier_groups mg
                LEFT JOIN product_modifier_groups pmg ON mg.id = pmg.modifier_group_id
                LEFT JOIN product p ON pmg.product_id = p.id
                WHERE mg.tenant_id = $1
            """

            params = [tenant_id]
            param_count = 2

            # Add filters
            if search:
                base_query += f" AND (LOWER(mg.name) LIKE LOWER(${param_count}) OR LOWER(p.name) LIKE LOWER(${param_count}))"
                count_query += f" AND (LOWER(mg.name) LIKE LOWER(${param_count}) OR LOWER(p.name) LIKE LOWER(${param_count}))"
                params.append(f"%{search}%")
                param_count += 1

            if product_id:
                base_query += f" AND pmg.product_id = ${param_count}"
                count_query += f" AND pmg.product_id = ${param_count}"
                params.append(product_id)
                param_count += 1

            # Add pagination
            offset = (page - 1) * limit
            base_query += f" ORDER BY mg.sort_order, mg.created_at DESC LIMIT ${param_count} OFFSET ${param_count + 1}"

            # Execute queries
            groups_data = await conn.fetch(base_query, *params, limit, offset)
            count_result = await conn.fetchrow(count_query, *params)

            total = count_result['count']

            # Convert to ModifierGroup models
            groups = []
            for row in groups_data:
                group_dict = dict(row)

                # Fetch associated products from junction table
                products_query = """
                    SELECT p.id, p.name
                    FROM product_modifier_groups pmg
                    JOIN product p ON pmg.product_id = p.id
                    WHERE pmg.modifier_group_id = $1
                    ORDER BY p.name
                """
                products_rows = await conn.fetch(products_query, row['id'])
                group_dict['products'] = [ProductInfo(id=r['id'], name=r['name']) for r in products_rows]

                # Fetch modifiers for each group with ingredient info
                modifiers_query = """
                    SELECT
                        m.id,
                        m.modifier_group_id,
                        m.name,
                        m.price,
                        m.max_limit,
                        m.is_default,
                        m.is_available,
                        m.sort_order,
                        m.created_at,
                        m.updated_at,
                        m.ingredient_id,
                        m.ingredient_quantity,
                        m.ingredient_unit,
                        i.name as ingredient_name,
                        i.unit as ingredient_base_unit,
                        i.costo_unitario,
                        i.controla_inventario
                    FROM modifiers m
                    LEFT JOIN ingredients i ON m.ingredient_id = i.id
                    WHERE m.modifier_group_id = $1
                    ORDER BY m.sort_order, m.name
                """
                modifier_rows = await conn.fetch(modifiers_query, row['id'])

                # Build modifiers with ingredient info
                modifiers = []
                for r in modifier_rows:
                    mod_dict = {
                        'id': r['id'],
                        'modifier_group_id': r['modifier_group_id'],
                        'name': r['name'],
                        'price': r['price'],
                        'max_limit': r['max_limit'],
                        'is_default': r['is_default'],
                        'is_available': r['is_available'],
                        'sort_order': r['sort_order'],
                        'created_at': r['created_at'],
                        'updated_at': r['updated_at'],
                        'ingredient_id': r['ingredient_id'],
                        'ingredient_quantity': r['ingredient_quantity'],
                        'ingredient_unit': r['ingredient_unit'],
                    }
                    if r['ingredient_id']:
                        mod_dict['ingredient'] = IngredientInfo(
                            id=r['ingredient_id'],
                            name=r['ingredient_name'],
                            unit=r['ingredient_base_unit'],
                            costo_unitario=r['costo_unitario'],
                            controla_inventario=r['controla_inventario'] or False
                        )
                    modifiers.append(Modifier(**mod_dict))

                group_dict['modifiers'] = modifiers

                groups.append(ModifierGroup(**group_dict))

            return ModifierGroupsListResponse(
                success=True,
                total=total,
                data=groups
            )

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error fetching modifier groups: {str(e)}")
        raise APIError(f"Error fetching modifier groups: {str(e)}", status_code=500)


async def get_modifier_group_stats(request: Request) -> ModifierGroupStats:
    """Get modifier group statistics. Now counts products from junction table."""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            stats_query = """
                SELECT
                    COUNT(DISTINCT mg.id) as total_groups,
                    COUNT(DISTINCT m.id) as total_modifiers,
                    COUNT(DISTINCT pmg.product_id) as products_with_modifiers
                FROM modifier_groups mg
                LEFT JOIN modifiers m ON mg.id = m.modifier_group_id
                LEFT JOIN product_modifier_groups pmg ON mg.id = pmg.modifier_group_id
                WHERE mg.tenant_id = $1
            """

            stats_row = await conn.fetchrow(stats_query, tenant_id)

            return ModifierGroupStats(**dict(stats_row))

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error fetching modifier group stats: {str(e)}")
        raise APIError(f"Error fetching modifier group stats: {str(e)}", status_code=500)


async def update_modifier_group(
    request: Request,
    group_id: UUID,
    group_data: ModifierGroupUpdate
) -> ModifierGroupResponse:
    """Updates a modifier group with its modifiers and product associations in a single transaction."""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Verify group exists and belongs to tenant
            verify_query = "SELECT id FROM modifier_groups WHERE id = $1 AND tenant_id = $2"
            group_exists = await conn.fetchrow(verify_query, group_id, tenant_id)

            if not group_exists:
                raise HTTPException(status_code=404, detail="Modifier group not found")

            # Start transaction
            async with conn.transaction():
                # 1. Build update query dynamically (excluding product_ids and modifiers)
                update_fields = []
                update_values = []
                param_count = 1

                for field, value in group_data.model_dump(exclude={'modifiers', 'product_ids'}, exclude_unset=True).items():
                    if value is not None:
                        update_fields.append(f"{field} = ${param_count}")
                        update_values.append(value)
                        param_count += 1

                if update_fields:
                    update_fields.append(f"updated_at = NOW()")
                    update_query = f"""
                        UPDATE modifier_groups
                        SET {', '.join(update_fields)}
                        WHERE id = ${param_count} AND tenant_id = ${param_count + 1}
                    """
                    update_values.extend([group_id, tenant_id])
                    await conn.execute(update_query, *update_values)

                # 2. Update product associations if provided
                if group_data.product_ids is not None:
                    # Delete existing associations
                    delete_assoc_query = "DELETE FROM product_modifier_groups WHERE modifier_group_id = $1"
                    await conn.execute(delete_assoc_query, group_id)

                    # Insert new associations
                    if group_data.product_ids:
                        assoc_query = """
                            INSERT INTO product_modifier_groups (product_id, modifier_group_id, tenant_id)
                            VALUES ($1, $2, $3)
                        """
                        for product_id in group_data.product_ids:
                            await conn.execute(assoc_query, product_id, group_id, tenant_id)

                # 3. Update modifiers if provided (upsert to preserve order history)
                if group_data.modifiers is not None:
                    # Get existing modifiers
                    existing_modifiers = await conn.fetch(
                        "SELECT id, name FROM modifiers WHERE modifier_group_id = $1",
                        group_id
                    )
                    existing_names = {row['name']: row['id'] for row in existing_modifiers}
                    existing_ids = set(existing_names.values())
                    modifiers_to_keep = set()

                    for modifier in group_data.modifiers:
                        if modifier.name in existing_names:
                            # UPDATE existing modifier (including ingredient fields)
                            mod_id = existing_names[modifier.name]
                            modifiers_to_keep.add(mod_id)
                            await conn.execute(
                                """
                                UPDATE modifiers SET
                                    price = $2, max_limit = $3, is_default = $4,
                                    is_available = $5, sort_order = $6,
                                    ingredient_id = $7, ingredient_quantity = $8, ingredient_unit = $9
                                WHERE id = $1
                                """,
                                mod_id,
                                modifier.price,
                                modifier.max_limit,
                                modifier.is_default,
                                modifier.is_available,
                                modifier.sort_order,
                                modifier.ingredient_id,
                                modifier.ingredient_quantity,
                                modifier.ingredient_unit
                            )
                        else:
                            # INSERT new modifier (with ingredient fields)
                            await conn.execute(
                                """
                                INSERT INTO modifiers (
                                    modifier_group_id, name, price, max_limit,
                                    is_default, is_available, sort_order,
                                    ingredient_id, ingredient_quantity, ingredient_unit
                                )
                                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                                """,
                                group_id,
                                modifier.name,
                                modifier.price,
                                modifier.max_limit,
                                modifier.is_default,
                                modifier.is_available,
                                modifier.sort_order,
                                modifier.ingredient_id,
                                modifier.ingredient_quantity,
                                modifier.ingredient_unit
                            )

                    # Soft-delete removed modifiers (preserve order history)
                    modifiers_to_disable = existing_ids - modifiers_to_keep
                    if modifiers_to_disable:
                        await conn.execute(
                            "UPDATE modifiers SET is_available = false WHERE id = ANY($1::uuid[])",
                            list(modifiers_to_disable)
                        )

                # 4. Get complete updated group
                return await get_modifier_group_by_id(request, group_id, conn)

    except HTTPException:
        raise
    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error updating modifier group: {str(e)}")
        raise APIError(f"Error updating modifier group: {str(e)}", status_code=500)


async def delete_modifier_group(
    request: Request,
    group_id: UUID
) -> dict:
    """Deletes a modifier group and its modifiers."""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Verify group exists and belongs to tenant
            verify_query = "SELECT id FROM modifier_groups WHERE id = $1 AND tenant_id = $2"
            group_exists = await conn.fetchrow(verify_query, group_id, tenant_id)

            if not group_exists:
                raise HTTPException(status_code=404, detail="Modifier group not found")

            # Start transaction
            async with conn.transaction():
                # Delete modifiers first (foreign key constraint)
                delete_modifiers_query = "DELETE FROM modifiers WHERE modifier_group_id = $1"
                await conn.execute(delete_modifiers_query, group_id)

                # Delete group
                delete_group_query = "DELETE FROM modifier_groups WHERE id = $1 AND tenant_id = $2"
                await conn.execute(delete_group_query, group_id, tenant_id)

                return {
                    "success": True,
                    "message": "Modifier group deleted successfully"
                }

    except HTTPException:
        raise
    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error deleting modifier group: {str(e)}")
        raise APIError(f"Error deleting modifier group: {str(e)}", status_code=500)

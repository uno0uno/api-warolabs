from typing import List, Optional
from uuid import UUID
from fastapi import Request, Response, HTTPException
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError, APIError
from app.models.modifier import (
    ModifierGroup, ModifierGroupCreate, ModifierGroupUpdate,
    ModifierGroupsListResponse, ModifierGroupResponse, ModifierGroupStats,
    Modifier
)
import logging

logger = logging.getLogger(__name__)

async def create_modifier_group(
    request: Request,
    group_data: ModifierGroupCreate
) -> ModifierGroupResponse:
    """
    Creates a modifier group with its modifiers in a single transaction.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id or group_data.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                # 1. Insert modifier group
                group_query = """
                    INSERT INTO modifier_groups (
                        product_id, tenant_id, name, min_qty, max_qty,
                        is_required, sort_order
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    RETURNING id, created_at, updated_at
                """
                group_result = await conn.fetchrow(
                    group_query,
                    group_data.product_id,
                    tenant_id,
                    group_data.name,
                    group_data.min_qty,
                    group_data.max_qty,
                    group_data.is_required,
                    group_data.sort_order
                )

                group_id = group_result['id']

                # 2. Insert modifiers
                if group_data.modifiers:
                    modifier_query = """
                        INSERT INTO modifiers (
                            modifier_group_id, name, price, max_limit,
                            is_default, is_available, sort_order
                        )
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
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
                            modifier.sort_order
                        )

                # 3. Get complete group with modifiers
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
    """Get a single modifier group with its modifiers"""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async def _fetch_group(connection):
            # Get group with product name
            group_query = """
                SELECT
                    mg.id,
                    mg.product_id,
                    mg.tenant_id,
                    mg.name,
                    mg.min_qty,
                    mg.max_qty,
                    mg.is_required,
                    mg.sort_order,
                    mg.created_at,
                    mg.updated_at,
                    p.name as product_name
                FROM modifier_groups mg
                LEFT JOIN product p ON mg.product_id = p.id
                WHERE mg.id = $1 AND mg.tenant_id = $2
            """

            group_row = await connection.fetchrow(group_query, group_id, tenant_id)

            if not group_row:
                raise HTTPException(status_code=404, detail="Modifier group not found")

            # Get modifiers
            modifiers_query = """
                SELECT
                    id,
                    modifier_group_id,
                    name,
                    price,
                    max_limit,
                    is_default,
                    is_available,
                    sort_order,
                    created_at,
                    updated_at
                FROM modifiers
                WHERE modifier_group_id = $1
                ORDER BY sort_order, name
            """

            modifier_rows = await connection.fetch(modifiers_query, group_id)

            # Build group dict
            group_dict = dict(group_row)
            group_dict['modifiers'] = [Modifier(**dict(row)) for row in modifier_rows]

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
    """Get list of modifier groups with filters"""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Base query
            base_query = """
                SELECT
                    mg.id,
                    mg.product_id,
                    mg.tenant_id,
                    mg.name,
                    mg.min_qty,
                    mg.max_qty,
                    mg.is_required,
                    mg.sort_order,
                    mg.created_at,
                    mg.updated_at,
                    p.name as product_name
                FROM modifier_groups mg
                LEFT JOIN product p ON mg.product_id = p.id
                WHERE mg.tenant_id = $1
            """

            count_query = "SELECT COUNT(*) FROM modifier_groups WHERE tenant_id = $1"

            params = [tenant_id]
            param_count = 2

            # Add filters
            if search:
                base_query += f" AND (LOWER(mg.name) LIKE LOWER(${param_count}) OR LOWER(p.name) LIKE LOWER(${param_count}))"
                count_query += f" AND EXISTS (SELECT 1 FROM modifier_groups mg2 LEFT JOIN product p2 ON mg2.product_id = p2.id WHERE mg2.id = modifier_groups.id AND (LOWER(mg2.name) LIKE LOWER(${param_count}) OR LOWER(p2.name) LIKE LOWER(${param_count})))"
                params.append(f"%{search}%")
                param_count += 1

            if product_id:
                base_query += f" AND mg.product_id = ${param_count}"
                count_query += f" AND product_id = ${param_count}"
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

                # Fetch modifiers for each group
                modifiers_query = """
                    SELECT
                        id,
                        modifier_group_id,
                        name,
                        price,
                        max_limit,
                        is_default,
                        is_available,
                        sort_order,
                        created_at,
                        updated_at
                    FROM modifiers
                    WHERE modifier_group_id = $1
                    ORDER BY sort_order, name
                """
                modifier_rows = await conn.fetch(modifiers_query, row['id'])
                group_dict['modifiers'] = [Modifier(**dict(r)) for r in modifier_rows]

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
    """Get modifier group statistics"""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            stats_query = """
                SELECT
                    COUNT(DISTINCT mg.id) as total_groups,
                    COUNT(m.id) as total_modifiers,
                    COUNT(DISTINCT mg.product_id) as products_with_modifiers
                FROM modifier_groups mg
                LEFT JOIN modifiers m ON mg.id = m.modifier_group_id
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
    """Updates a modifier group with its modifiers in a single transaction."""
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
                # 1. Build update query dynamically
                update_fields = []
                update_values = []
                param_count = 1

                for field, value in group_data.dict(exclude={'modifiers'}, exclude_unset=True).items():
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

                # 2. Update modifiers if provided
                if group_data.modifiers is not None:
                    # Delete existing modifiers
                    delete_modifiers_query = "DELETE FROM modifiers WHERE modifier_group_id = $1"
                    await conn.execute(delete_modifiers_query, group_id)

                    # Insert new modifiers
                    if group_data.modifiers:
                        modifier_query = """
                            INSERT INTO modifiers (
                                modifier_group_id, name, price, max_limit,
                                is_default, is_available, sort_order
                            )
                            VALUES ($1, $2, $3, $4, $5, $6, $7)
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
                                modifier.sort_order
                            )

                # 3. Get complete updated group
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

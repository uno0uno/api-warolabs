from typing import List, Optional
from uuid import UUID
from decimal import Decimal
from fastapi import Request, Response, HTTPException
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError, APIError
from app.models.combo import (
    Combo, ComboCreate, ComboUpdate,
    CombosListResponse, ComboResponse, ComboStats,
    ComboItem
)
import logging

logger = logging.getLogger(__name__)

async def create_combo(
    request: Request,
    combo_data: ComboCreate
) -> ComboResponse:
    """
    Creates a combo product with its items in a single transaction.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id or combo_data.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                # 1. Insert combo as a product
                product_query = """
                    INSERT INTO product (
                        tenant_id, name, description, price, category_id,
                        is_available, is_combo, controla_stock
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, true, false)
                    RETURNING id, created_at, updated_at
                """
                product_result = await conn.fetchrow(
                    product_query,
                    tenant_id,
                    combo_data.name,
                    combo_data.description,
                    combo_data.price,
                    combo_data.category_id,
                    combo_data.is_available
                )

                combo_id = product_result['id']

                # 2. Insert combo items
                if combo_data.items:
                    item_query = """
                        INSERT INTO combo_items (
                            combo_product_id, item_product_id, quantity,
                            is_optional, is_customizable, sort_order,
                            individual_price, combo_price, discount_amount
                        )
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    """

                    for item in combo_data.items:
                        await conn.execute(
                            item_query,
                            combo_id,
                            item.item_product_id,
                            item.quantity,
                            item.is_optional,
                            item.is_customizable,
                            item.sort_order,
                            item.individual_price,
                            item.combo_price,
                            item.discount_amount
                        )

                # 3. Get complete combo with items
                return await get_combo_by_id(request, combo_id, conn)

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error creating combo: {str(e)}")
        raise APIError(f"Error creating combo: {str(e)}", status_code=500)


async def get_combo_by_id(
    request: Request,
    combo_id: UUID,
    conn=None
) -> ComboResponse:
    """Get a single combo with its items"""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async def _fetch_combo(connection):
            # Get combo with category name
            combo_query = """
                SELECT
                    p.id,
                    p.tenant_id,
                    p.name,
                    p.description,
                    p.price,
                    p.category_id,
                    p.is_available,
                    p.is_combo,
                    p.created_at,
                    p.updated_at,
                    c.name as category_name
                FROM product p
                LEFT JOIN categories c ON p.category_id = c.id
                WHERE p.id = $1 AND p.tenant_id = $2 AND p.is_combo = true
            """

            combo_row = await connection.fetchrow(combo_query, combo_id, tenant_id)

            if not combo_row:
                raise HTTPException(status_code=404, detail="Combo not found")

            # Get combo items with product names
            items_query = """
                SELECT
                    ci.id,
                    ci.combo_product_id,
                    ci.item_product_id,
                    ci.quantity,
                    ci.is_optional,
                    ci.is_customizable,
                    ci.sort_order,
                    ci.individual_price,
                    ci.combo_price,
                    ci.discount_amount,
                    ci.created_at,
                    ci.updated_at,
                    p.name as product_name
                FROM combo_items ci
                LEFT JOIN product p ON ci.item_product_id = p.id
                WHERE ci.combo_product_id = $1
                ORDER BY ci.sort_order, ci.created_at
            """

            item_rows = await connection.fetch(items_query, combo_id)

            # Build combo dict
            combo_dict = dict(combo_row)
            combo_dict['items'] = [ComboItem(**dict(row)) for row in item_rows]

            # Calculate totals
            total_individual = sum(Decimal(str(item['individual_price'] or 0)) * Decimal(str(item['quantity'])) for item in item_rows)
            total_combo = sum(Decimal(str(item['combo_price'] or 0)) * Decimal(str(item['quantity'])) for item in item_rows)
            combo_dict['total_individual_price'] = float(total_individual)
            combo_dict['total_savings'] = float(total_individual - total_combo) if total_combo > 0 else None

            return ComboResponse(data=Combo(**combo_dict))

        if conn:
            return await _fetch_combo(conn)
        else:
            async with get_db_connection() as connection:
                return await _fetch_combo(connection)

    except HTTPException:
        raise
    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error fetching combo: {str(e)}")
        raise APIError(f"Error fetching combo: {str(e)}", status_code=500)


async def get_combos_list(
    request: Request,
    response: Response,
    page: int = 1,
    limit: int = 50,
    search: Optional[str] = None,
    category_id: Optional[UUID] = None,
    is_available: Optional[bool] = None
) -> CombosListResponse:
    """Get list of combos with filters"""
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
                    p.tenant_id,
                    p.name,
                    p.description,
                    p.price,
                    p.category_id,
                    p.is_available,
                    p.is_combo,
                    p.created_at,
                    p.updated_at,
                    c.name as category_name
                FROM product p
                LEFT JOIN categories c ON p.category_id = c.id
                WHERE p.tenant_id = $1 AND p.is_combo = true
            """

            count_query = "SELECT COUNT(*) FROM product WHERE tenant_id = $1 AND is_combo = true"

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

            # Add pagination
            offset = (page - 1) * limit
            base_query += f" ORDER BY p.created_at DESC LIMIT ${param_count} OFFSET ${param_count + 1}"

            # Execute queries
            combos_data = await conn.fetch(base_query, *params, limit, offset)
            count_result = await conn.fetchrow(count_query, *params)

            total = count_result['count']

            # Convert to Combo models and fetch items
            combos = []
            for row in combos_data:
                combo_dict = dict(row)

                # Fetch items for each combo
                items_query = """
                    SELECT
                        ci.id,
                        ci.combo_product_id,
                        ci.item_product_id,
                        ci.quantity,
                        ci.is_optional,
                        ci.is_customizable,
                        ci.sort_order,
                        ci.individual_price,
                        ci.combo_price,
                        ci.discount_amount,
                        ci.created_at,
                        ci.updated_at,
                        p.name as product_name
                    FROM combo_items ci
                    LEFT JOIN product p ON ci.item_product_id = p.id
                    WHERE ci.combo_product_id = $1
                    ORDER BY ci.sort_order, ci.created_at
                """
                item_rows = await conn.fetch(items_query, row['id'])
                combo_dict['items'] = [ComboItem(**dict(r)) for r in item_rows]

                # Calculate totals
                total_individual = sum(Decimal(str(item['individual_price'] or 0)) * Decimal(str(item['quantity'])) for item in item_rows)
                total_combo = sum(Decimal(str(item['combo_price'] or 0)) * Decimal(str(item['quantity'])) for item in item_rows)
                combo_dict['total_individual_price'] = float(total_individual)
                combo_dict['total_savings'] = float(total_individual - total_combo) if total_combo > 0 else None

                combos.append(Combo(**combo_dict))

            return CombosListResponse(
                success=True,
                total=total,
                data=combos
            )

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error fetching combos: {str(e)}")
        raise APIError(f"Error fetching combos: {str(e)}", status_code=500)


async def get_combo_stats(request: Request) -> ComboStats:
    """Get combo statistics"""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            stats_query = """
                SELECT
                    COUNT(DISTINCT p.id) as total_combos,
                    COUNT(DISTINCT CASE WHEN p.is_available = true THEN p.id END) as active_combos,
                    COUNT(ci.id) as total_items,
                    AVG(
                        CASE
                            WHEN ci.individual_price IS NOT NULL AND ci.combo_price IS NOT NULL
                            THEN (ci.individual_price - ci.combo_price) * ci.quantity
                            ELSE 0
                        END
                    ) as avg_savings
                FROM product p
                LEFT JOIN combo_items ci ON p.id = ci.combo_product_id
                WHERE p.tenant_id = $1 AND p.is_combo = true
            """

            stats_row = await conn.fetchrow(stats_query, tenant_id)

            return ComboStats(**dict(stats_row))

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error fetching combo stats: {str(e)}")
        raise APIError(f"Error fetching combo stats: {str(e)}", status_code=500)


async def update_combo(
    request: Request,
    combo_id: UUID,
    combo_data: ComboUpdate
) -> ComboResponse:
    """Updates a combo with its items in a single transaction."""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Verify combo exists and belongs to tenant
            verify_query = "SELECT id FROM product WHERE id = $1 AND tenant_id = $2 AND is_combo = true"
            combo_exists = await conn.fetchrow(verify_query, combo_id, tenant_id)

            if not combo_exists:
                raise HTTPException(status_code=404, detail="Combo not found")

            # Start transaction
            async with conn.transaction():
                # 1. Build update query dynamically
                update_fields = []
                update_values = []
                param_count = 1

                for field, value in combo_data.dict(exclude={'items'}, exclude_unset=True).items():
                    if value is not None:
                        update_fields.append(f"{field} = ${param_count}")
                        update_values.append(value)
                        param_count += 1

                if update_fields:
                    update_fields.append(f"updated_at = NOW()")
                    update_query = f"""
                        UPDATE product
                        SET {', '.join(update_fields)}
                        WHERE id = ${param_count} AND tenant_id = ${param_count + 1}
                    """
                    update_values.extend([combo_id, tenant_id])
                    await conn.execute(update_query, *update_values)

                # 2. Update combo items if provided
                if combo_data.items is not None:
                    # Delete existing items
                    delete_items_query = "DELETE FROM combo_items WHERE combo_product_id = $1"
                    await conn.execute(delete_items_query, combo_id)

                    # Insert new items
                    if combo_data.items:
                        item_query = """
                            INSERT INTO combo_items (
                                combo_product_id, item_product_id, quantity,
                                is_optional, is_customizable, sort_order,
                                individual_price, combo_price, discount_amount
                            )
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                        """

                        for item in combo_data.items:
                            await conn.execute(
                                item_query,
                                combo_id,
                                item.item_product_id,
                                item.quantity,
                                item.is_optional,
                                item.is_customizable,
                                item.sort_order,
                                item.individual_price,
                                item.combo_price,
                                item.discount_amount
                            )

                # 3. Get complete updated combo
                return await get_combo_by_id(request, combo_id, conn)

    except HTTPException:
        raise
    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error updating combo: {str(e)}")
        raise APIError(f"Error updating combo: {str(e)}", status_code=500)


async def delete_combo(
    request: Request,
    combo_id: UUID
) -> dict:
    """Deletes a combo and its items."""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Verify combo exists and belongs to tenant
            verify_query = "SELECT id FROM product WHERE id = $1 AND tenant_id = $2 AND is_combo = true"
            combo_exists = await conn.fetchrow(verify_query, combo_id, tenant_id)

            if not combo_exists:
                raise HTTPException(status_code=404, detail="Combo not found")

            # Start transaction
            async with conn.transaction():
                # Delete items first (foreign key constraint)
                delete_items_query = "DELETE FROM combo_items WHERE combo_product_id = $1"
                await conn.execute(delete_items_query, combo_id)

                # Delete combo product
                delete_combo_query = "DELETE FROM product WHERE id = $1 AND tenant_id = $2"
                await conn.execute(delete_combo_query, combo_id, tenant_id)

                return {
                    "success": True,
                    "message": "Combo deleted successfully"
                }

    except HTTPException:
        raise
    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error deleting combo: {str(e)}")
        raise APIError(f"Error deleting combo: {str(e)}", status_code=500)

"""
Inventory Service
Handles inventory queries, movements, and stock management
"""
from typing import Optional, List, Dict, Any
from uuid import UUID
from fastapi import Request, Response, HTTPException
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError
import logging

logger = logging.getLogger(__name__)


async def get_inventory_stock(
    request: Request,
    response: Response,
    limit: int = 250,
    offset: int = 0,
    search: Optional[str] = None,
    status_filter: Optional[str] = None,  # 'low', 'critical', 'ok', 'all'
    sort_field: str = "current_stock",
    sort_direction: str = "desc"
) -> Dict[str, Any]:
    """
    Get current inventory stock with stats

    Args:
        request: FastAPI request
        response: FastAPI response
        limit: Number of records to return
        offset: Number of records to skip
        search: Search term for ingredient name
        status_filter: Filter by stock status (low, critical, ok, all)
        sort_field: Field to sort by
        sort_direction: Sort direction (asc, desc)

    Returns:
        Dictionary with inventory data and stats
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Build base query
            base_query = """
                SELECT
                    ti.id,
                    ti.ingredient_id,
                    i.name as ingredient_name,
                    i.unit,
                    i.category,
                    ti.current_stock,
                    ti.minimum_stock,
                    ti.maximum_stock,
                    ti.last_updated,
                    ti.location,
                    ti.lote_actual,
                    ti.fecha_vencimiento,
                    -- Calculate status
                    CASE
                        WHEN ti.current_stock = 0 THEN 'critical'
                        WHEN ti.current_stock <= ti.minimum_stock THEN 'low'
                        ELSE 'ok'
                    END as status,
                    -- Calculate stock percentage
                    CASE
                        WHEN ti.maximum_stock IS NOT NULL AND ti.maximum_stock > 0
                        THEN ROUND((ti.current_stock / ti.maximum_stock * 100)::numeric, 2)
                        ELSE NULL
                    END as stock_percentage,
                    -- Get cost from latest movement
                    (
                        SELECT tim.cost_per_unit
                        FROM tenant_ingredient_movements tim
                        WHERE tim.ingredient_id = ti.ingredient_id
                          AND tim.tenant_id = ti.tenant_id
                          AND tim.cost_per_unit IS NOT NULL
                        ORDER BY tim.created_at DESC
                        LIMIT 1
                    ) as unit_cost,
                    -- Calculate total value
                    ti.current_stock * COALESCE(
                        (
                            SELECT tim.cost_per_unit
                            FROM tenant_ingredient_movements tim
                            WHERE tim.ingredient_id = ti.ingredient_id
                              AND tim.tenant_id = ti.tenant_id
                              AND tim.cost_per_unit IS NOT NULL
                            ORDER BY tim.created_at DESC
                            LIMIT 1
                        ), 0
                    ) as total_value
                FROM tenant_inventory ti
                JOIN ingredients i ON ti.ingredient_id = i.id
                WHERE ti.tenant_id = $1
            """

            count_query = """
                SELECT COUNT(*) as total
                FROM tenant_inventory ti
                JOIN ingredients i ON ti.ingredient_id = i.id
                WHERE ti.tenant_id = $1
            """

            params = [tenant_id]
            param_count = 2

            # Add search filter
            if search:
                base_query += f" AND LOWER(i.name) LIKE LOWER(${param_count})"
                count_query += f" AND LOWER(i.name) LIKE LOWER(${param_count})"
                params.append(f"%{search}%")
                param_count += 1

            # Add status filter
            if status_filter and status_filter != 'all':
                if status_filter == 'critical':
                    base_query += " AND ti.current_stock = 0"
                    count_query += " AND ti.current_stock = 0"
                elif status_filter == 'low':
                    base_query += " AND ti.current_stock > 0 AND ti.current_stock <= ti.minimum_stock"
                    count_query += " AND ti.current_stock > 0 AND ti.current_stock <= ti.minimum_stock"
                elif status_filter == 'ok':
                    base_query += " AND ti.current_stock > ti.minimum_stock"
                    count_query += " AND ti.current_stock > ti.minimum_stock"

            # Add sorting
            valid_sort_fields = {
                'ingredient_name': 'i.name',
                'current_stock': 'ti.current_stock',
                'minimum_stock': 'ti.minimum_stock',
                'total_value': 'total_value',
                'last_updated': 'ti.last_updated'
            }
            sort_column = valid_sort_fields.get(sort_field, 'ti.current_stock')
            sort_dir = 'DESC' if sort_direction.lower() == 'desc' else 'ASC'

            base_query += f" ORDER BY {sort_column} {sort_dir}"

            # Add pagination
            base_query += f" LIMIT ${param_count} OFFSET ${param_count + 1}"
            params.extend([limit, offset])

            # Execute queries
            inventory_data = await conn.fetch(base_query, *params)
            count_result = await conn.fetchrow(count_query, *params[:-2])

            # Get stats
            stats_query = """
                SELECT
                    COUNT(*) as total_ingredients,
                    COUNT(*) FILTER (WHERE ti.current_stock = 0) as critical_count,
                    COUNT(*) FILTER (WHERE ti.current_stock > 0 AND ti.current_stock <= ti.minimum_stock) as low_stock_count,
                    COUNT(*) FILTER (WHERE ti.current_stock > ti.minimum_stock) as ok_count,
                    SUM(
                        ti.current_stock * COALESCE(
                            (
                                SELECT tim.cost_per_unit
                                FROM tenant_ingredient_movements tim
                                WHERE tim.ingredient_id = ti.ingredient_id
                                  AND tim.tenant_id = ti.tenant_id
                                  AND tim.cost_per_unit IS NOT NULL
                                ORDER BY tim.created_at DESC
                                LIMIT 1
                            ), 0
                        )
                    ) as total_inventory_value
                FROM tenant_inventory ti
                WHERE ti.tenant_id = $1
            """
            stats = await conn.fetchrow(stats_query, tenant_id)

            # Format inventory data
            inventory_list = []
            for row in inventory_data:
                inventory_list.append({
                    "id": str(row['id']),
                    "ingredient_id": str(row['ingredient_id']),
                    "ingredient_name": row['ingredient_name'],
                    "unit": row['unit'],
                    "category": row['category'],
                    "current_stock": float(row['current_stock']) if row['current_stock'] else 0,
                    "minimum_stock": float(row['minimum_stock']) if row['minimum_stock'] else 0,
                    "maximum_stock": float(row['maximum_stock']) if row['maximum_stock'] else None,
                    "last_updated": row['last_updated'].isoformat() if row['last_updated'] else None,
                    "location": row['location'],
                    "lote_actual": row['lote_actual'],
                    "fecha_vencimiento": row['fecha_vencimiento'].isoformat() if row['fecha_vencimiento'] else None,
                    "status": row['status'],
                    "stock_percentage": float(row['stock_percentage']) if row['stock_percentage'] else None,
                    "unit_cost": float(row['unit_cost']) if row['unit_cost'] else None,
                    "total_value": float(row['total_value']) if row['total_value'] else 0
                })

            return {
                "success": True,
                "data": inventory_list,
                "total": count_result['total'],
                "limit": limit,
                "offset": offset,
                "stats": {
                    "total_ingredients": stats['total_ingredients'],
                    "critical_count": stats['critical_count'],
                    "low_stock_count": stats['low_stock_count'],
                    "ok_count": stats['ok_count'],
                    "total_inventory_value": float(stats['total_inventory_value']) if stats['total_inventory_value'] else 0
                }
            }

    except AuthenticationError:
        raise
    except Exception as e:
        logger.error(f"Error getting inventory stock: {str(e)}")
        raise HTTPException(status_code=500, detail="Error interno del servidor")


async def get_inventory_movements(
    request: Request,
    response: Response,
    limit: int = 100,
    offset: int = 0,
    ingredient_id: Optional[UUID] = None,
    movement_type: Optional[str] = None,  # 'purchase', 'consumption', 'adjustment', etc.
    start_date: Optional[str] = None,
    end_date: Optional[str] = None
) -> Dict[str, Any]:
    """
    Get inventory movements history

    Args:
        request: FastAPI request
        response: FastAPI response
        limit: Number of records to return
        offset: Number of records to skip
        ingredient_id: Filter by specific ingredient
        movement_type: Filter by movement type
        start_date: Filter by start date
        end_date: Filter by end date

    Returns:
        Dictionary with movements data
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Build query
            base_query = """
                SELECT
                    tim.id,
                    tim.ingredient_id,
                    i.name as ingredient_name,
                    i.unit,
                    tim.movement_type,
                    tim.quantity_change,
                    tim.previous_stock,
                    tim.new_stock,
                    tim.cost_per_unit,
                    tim.reference_table,
                    tim.reference_id,
                    tim.reason,
                    tim.notes,
                    tim.created_by,
                    tim.created_at,
                    p.name as created_by_name,
                    -- Get reference details if it's a purchase
                    CASE
                        WHEN tim.reference_table = 'tenant_purchases' THEN
                            (SELECT purchase_number FROM tenant_purchases WHERE id = tim.reference_id)
                        WHEN tim.reference_table = 'orders' THEN
                            (SELECT order_number::text FROM orders WHERE id = tim.reference_id)
                        ELSE NULL
                    END as reference_number
                FROM tenant_ingredient_movements tim
                JOIN ingredients i ON tim.ingredient_id = i.id
                LEFT JOIN profile p ON tim.created_by = p.id
                WHERE tim.tenant_id = $1
            """

            count_query = """
                SELECT COUNT(*) as total
                FROM tenant_ingredient_movements tim
                WHERE tim.tenant_id = $1
            """

            params = [tenant_id]
            param_count = 2

            # Add filters
            if ingredient_id:
                base_query += f" AND tim.ingredient_id = ${param_count}"
                count_query += f" AND tim.ingredient_id = ${param_count}"
                params.append(ingredient_id)
                param_count += 1

            if movement_type:
                base_query += f" AND tim.movement_type = ${param_count}"
                count_query += f" AND tim.movement_type = ${param_count}"
                params.append(movement_type)
                param_count += 1

            if start_date:
                base_query += f" AND tim.created_at >= ${param_count}::timestamp"
                count_query += f" AND tim.created_at >= ${param_count}::timestamp"
                params.append(start_date)
                param_count += 1

            if end_date:
                base_query += f" AND tim.created_at <= ${param_count}::timestamp"
                count_query += f" AND tim.created_at <= ${param_count}::timestamp"
                params.append(end_date)
                param_count += 1

            # Add sorting and pagination
            base_query += f" ORDER BY tim.created_at DESC LIMIT ${param_count} OFFSET ${param_count + 1}"
            params.extend([limit, offset])

            # Execute queries
            movements_data = await conn.fetch(base_query, *params)
            count_result = await conn.fetchrow(count_query, *params[:-2])

            # Format movements data
            movements_list = []
            for row in movements_data:
                movements_list.append({
                    "id": str(row['id']),
                    "ingredient_id": str(row['ingredient_id']),
                    "ingredient_name": row['ingredient_name'],
                    "unit": row['unit'],
                    "movement_type": row['movement_type'],
                    "quantity_change": float(row['quantity_change']),
                    "previous_stock": float(row['previous_stock']) if row['previous_stock'] else 0,
                    "new_stock": float(row['new_stock']) if row['new_stock'] else 0,
                    "cost_per_unit": float(row['cost_per_unit']) if row['cost_per_unit'] else None,
                    "reference_table": row['reference_table'],
                    "reference_id": str(row['reference_id']) if row['reference_id'] else None,
                    "reference_number": row['reference_number'],
                    "reason": row['reason'],
                    "notes": row['notes'],
                    "created_by": str(row['created_by']) if row['created_by'] else None,
                    "created_by_name": row['created_by_name'],
                    "created_at": row['created_at'].isoformat() if row['created_at'] else None
                })

            return {
                "success": True,
                "data": movements_list,
                "total": count_result['total'],
                "limit": limit,
                "offset": offset
            }

    except AuthenticationError:
        raise
    except Exception as e:
        logger.error(f"Error getting inventory movements: {str(e)}")
        raise HTTPException(status_code=500, detail="Error interno del servidor")


async def get_stock_by_ingredient(
    request: Request,
    response: Response,
    ingredient_id: UUID
) -> Dict[str, Any]:
    """
    Get current stock for a specific ingredient

    Args:
        request: FastAPI request
        response: FastAPI response
        ingredient_id: ID of the ingredient

    Returns:
        Dictionary with ingredient stock data
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Get stock data
            query = """
                SELECT
                    ti.id,
                    ti.ingredient_id,
                    i.name as ingredient_name,
                    i.unit,
                    i.category,
                    ti.current_stock,
                    ti.minimum_stock,
                    ti.maximum_stock,
                    ti.last_updated,
                    ti.location,
                    ti.lote_actual,
                    ti.fecha_vencimiento,
                    -- Get latest cost
                    (
                        SELECT tim.cost_per_unit
                        FROM tenant_ingredient_movements tim
                        WHERE tim.ingredient_id = ti.ingredient_id
                          AND tim.tenant_id = ti.tenant_id
                          AND tim.cost_per_unit IS NOT NULL
                        ORDER BY tim.created_at DESC
                        LIMIT 1
                    ) as unit_cost
                FROM tenant_inventory ti
                JOIN ingredients i ON ti.ingredient_id = i.id
                WHERE ti.tenant_id = $1 AND ti.ingredient_id = $2
            """

            stock_data = await conn.fetchrow(query, tenant_id, ingredient_id)

            if not stock_data:
                # If no record exists, create one
                insert_query = """
                    INSERT INTO tenant_inventory (tenant_id, ingredient_id, current_stock, minimum_stock)
                    VALUES ($1, $2, 0, 0)
                    RETURNING id
                """
                await conn.fetchrow(insert_query, tenant_id, ingredient_id)

                # Fetch again
                stock_data = await conn.fetchrow(query, tenant_id, ingredient_id)

            return {
                "success": True,
                "id": str(stock_data['id']),
                "ingredient_id": str(stock_data['ingredient_id']),
                "ingredient_name": stock_data['ingredient_name'],
                "unit": stock_data['unit'],
                "category": stock_data['category'],
                "current_stock": float(stock_data['current_stock']) if stock_data['current_stock'] else 0,
                "minimum_stock": float(stock_data['minimum_stock']) if stock_data['minimum_stock'] else 0,
                "maximum_stock": float(stock_data['maximum_stock']) if stock_data['maximum_stock'] else None,
                "last_updated": stock_data['last_updated'].isoformat() if stock_data['last_updated'] else None,
                "location": stock_data['location'],
                "lote_actual": stock_data['lote_actual'],
                "fecha_vencimiento": stock_data['fecha_vencimiento'].isoformat() if stock_data['fecha_vencimiento'] else None,
                "unit_cost": float(stock_data['unit_cost']) if stock_data['unit_cost'] else None
            }

    except AuthenticationError:
        raise
    except Exception as e:
        logger.error(f"Error getting ingredient stock: {str(e)}")
        raise HTTPException(status_code=500, detail="Error interno del servidor")


async def create_adjustment(
    request: Request,
    response: Response
) -> Dict[str, Any]:
    """
    Create a manual inventory adjustment

    Args:
        request: FastAPI request with body containing:
            - ingredient_id: UUID of the ingredient
            - quantity_change: Amount to adjust (+ or -)
            - reason: Reason for adjustment
            - source: Source of adjustment (default: manual_adjustment)
        response: FastAPI response

    Returns:
        Dictionary with created adjustment data
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        # Get request body
        body = await request.json()
        ingredient_id = UUID(body.get('ingredient_id'))
        quantity_change = float(body.get('quantity_change'))
        reason = body.get('reason', 'Manual adjustment')
        source = body.get('source', 'manual_adjustment')

        if not ingredient_id or quantity_change == 0:
            raise HTTPException(status_code=400, detail="ingredient_id and quantity_change are required")

        async with get_db_connection() as conn:
            # Start transaction
            async with conn.transaction():
                # Get ingredient unit
                ingredient_query = """
                    SELECT unit
                    FROM ingredients
                    WHERE id = $1
                """
                ingredient_row = await conn.fetchrow(ingredient_query, ingredient_id)

                if not ingredient_row:
                    raise HTTPException(status_code=404, detail="Ingrediente no encontrado")

                ingredient_unit = ingredient_row['unit']

                # Get current stock
                stock_query = """
                    SELECT current_stock
                    FROM tenant_inventory
                    WHERE tenant_id = $1 AND ingredient_id = $2
                    FOR UPDATE
                """
                stock_row = await conn.fetchrow(stock_query, tenant_id, ingredient_id)

                if not stock_row:
                    # Create inventory record if it doesn't exist
                    insert_query = """
                        INSERT INTO tenant_inventory (tenant_id, ingredient_id, current_stock, minimum_stock)
                        VALUES ($1, $2, 0, 0)
                    """
                    await conn.execute(insert_query, tenant_id, ingredient_id)
                    previous_stock = 0
                else:
                    previous_stock = float(stock_row['current_stock']) if stock_row['current_stock'] else 0

                # Calculate new stock
                new_stock = max(0, previous_stock + quantity_change)

                # Update stock
                update_query = """
                    UPDATE tenant_inventory
                    SET current_stock = $1, last_updated = NOW()
                    WHERE tenant_id = $2 AND ingredient_id = $3
                """
                await conn.execute(update_query, new_stock, tenant_id, ingredient_id)

                # Create movement record
                movement_query = """
                    INSERT INTO tenant_ingredient_movements (
                        tenant_id,
                        ingredient_id,
                        movement_type,
                        quantity_change,
                        unit,
                        previous_stock,
                        new_stock,
                        reference_table,
                        reason,
                        created_by,
                        created_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW())
                    RETURNING id, created_at
                """
                movement_row = await conn.fetchrow(
                    movement_query,
                    tenant_id,
                    ingredient_id,
                    'adjustment',
                    quantity_change,
                    ingredient_unit,
                    previous_stock,
                    new_stock,
                    source,
                    reason,
                    user_id
                )

                return {
                    "success": True,
                    "message": "Ajuste de inventario creado exitosamente",
                    "data": {
                        "id": str(movement_row['id']),
                        "ingredient_id": str(ingredient_id),
                        "quantity_change": quantity_change,
                        "previous_stock": previous_stock,
                        "new_stock": new_stock,
                        "reason": reason,
                        "created_at": movement_row['created_at'].isoformat()
                    }
                }

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating inventory adjustment: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error interno del servidor: {str(e)}")

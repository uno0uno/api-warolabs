"""
Inventory Service
Handles inventory queries, movements, and stock management
"""
from decimal import Decimal
from typing import Optional, List, Dict, Any
from uuid import UUID
from fastapi import Request, Response, HTTPException
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError
import logging

logger = logging.getLogger(__name__)

_QUANTITY_JSON_SCALE = Decimal("0.000001")
_MONEY_JSON_SCALE = Decimal("0.0001")
_TECHNICAL_COST_JSON_SCALE = Decimal("0.000001")
_PERCENT_JSON_SCALE = Decimal("0.01")


def _decimal_value(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    if value is None:
        return default
    return Decimal(str(value))


def _json_decimal(value: Any, scale: Decimal, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    quantized = Decimal(str(value)).quantize(scale)
    if quantized == 0:
        quantized = Decimal("0")
    return float(quantized)


async def _get_inventory_stock_for_tenant(
    tenant_id: str,
    *,
    limit: int = 250,
    offset: int = 0,
    search: Optional[str] = None,
    status_filter: Optional[str] = None,  # 'low', 'critical'/'negative', 'ok', 'zero', 'all'
    category: Optional[str] = None,
    unit: Optional[str] = None,
    sort_field: str = "current_stock",
    sort_direction: str = "desc"
) -> Dict[str, Any]:
    """
    Get current inventory stock with stats for a tenant.

    Args:
        tenant_id: Tenant to query
        limit: Number of records to return
        offset: Number of records to skip
        search: Search term for ingredient name
        status_filter: Filter by stock status (low, critical/negative, ok, zero, all)
        category: Filter by ingredient category
        unit: Filter by ingredient unit
        sort_field: Field to sort by
        sort_direction: Sort direction (asc, desc)

    Returns:
        Dictionary with inventory data and stats
    """
    try:
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # CTE to fetch latest cost per ingredient ONCE (avoids N correlated subqueries)
            cost_cte = """
                WITH latest_costs AS (
                    SELECT DISTINCT ON (ingredient_id)
                        ingredient_id,
                        cost_per_unit
                    FROM tenant_ingredient_movements
                    WHERE tenant_id = $1
                      AND cost_per_unit IS NOT NULL
                    ORDER BY ingredient_id, created_at DESC
                )
            """

            # Build base query using CTE join instead of correlated subqueries
            base_query = cost_cte + """
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
                    CASE
                        WHEN ti.current_stock < 0 THEN 'negative'
                        WHEN ti.current_stock = 0 THEN 'zero'
                        WHEN ti.current_stock > 0 AND ti.current_stock <= ti.minimum_stock THEN 'low'
                        ELSE 'ok'
                    END as status,
                    CASE
                        WHEN ti.maximum_stock IS NOT NULL AND ti.maximum_stock > 0
                        THEN ROUND((ti.current_stock / ti.maximum_stock * 100)::numeric, 2)
                        ELSE NULL
                    END as stock_percentage,
                    lc.cost_per_unit as unit_cost,
                    ti.current_stock * COALESCE(lc.cost_per_unit, 0) as total_value
                FROM tenant_inventory ti
                JOIN ingredients i ON ti.ingredient_id = i.id
                LEFT JOIN latest_costs lc ON lc.ingredient_id = ti.ingredient_id
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
                if status_filter in ('negative', 'critical'):
                    base_query += " AND ti.current_stock < 0"
                    count_query += " AND ti.current_stock < 0"
                elif status_filter == 'zero':
                    base_query += " AND ti.current_stock = 0"
                    count_query += " AND ti.current_stock = 0"
                elif status_filter == 'low':
                    base_query += " AND ti.current_stock > 0 AND ti.current_stock <= ti.minimum_stock"
                    count_query += " AND ti.current_stock > 0 AND ti.current_stock <= ti.minimum_stock"
                elif status_filter == 'ok':
                    base_query += " AND ti.current_stock > ti.minimum_stock"
                    count_query += " AND ti.current_stock > ti.minimum_stock"

            if category:
                base_query += f" AND i.category = ${param_count}"
                count_query += f" AND i.category = ${param_count}"
                params.append(category)
                param_count += 1

            if unit:
                base_query += f" AND i.unit = ${param_count}"
                count_query += f" AND i.unit = ${param_count}"
                params.append(unit)
                param_count += 1

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

            # Get stats using CTE join (avoids correlated subquery per row)
            stats_query = cost_cte + """
                SELECT
                    COUNT(*) as total_ingredients,
                    COUNT(*) FILTER (WHERE ti.current_stock < 0) as critical_count,
                    COUNT(*) FILTER (WHERE ti.current_stock = 0) as zero_count,
                    COUNT(*) FILTER (WHERE ti.current_stock > 0 AND ti.current_stock <= ti.minimum_stock) as low_stock_count,
                    COUNT(*) FILTER (WHERE ti.current_stock > ti.minimum_stock) as ok_count,
                    SUM(ti.current_stock * COALESCE(lc.cost_per_unit, 0)) as total_inventory_value
                FROM tenant_inventory ti
                LEFT JOIN latest_costs lc ON lc.ingredient_id = ti.ingredient_id
                WHERE ti.tenant_id = $1
            """
            stats = await conn.fetchrow(stats_query, tenant_id)

            filter_options_query = """
                SELECT
                    COALESCE((
                        SELECT ARRAY_AGG(category ORDER BY category)
                        FROM (
                            SELECT DISTINCT i.category
                            FROM tenant_inventory ti
                            JOIN ingredients i ON ti.ingredient_id = i.id
                            WHERE ti.tenant_id = $1
                              AND i.category IS NOT NULL
                        ) categories
                    ), ARRAY[]::text[]) AS categories,
                    COALESCE((
                        SELECT ARRAY_AGG(unit ORDER BY unit)
                        FROM (
                            SELECT DISTINCT i.unit
                            FROM tenant_inventory ti
                            JOIN ingredients i ON ti.ingredient_id = i.id
                            WHERE ti.tenant_id = $1
                              AND i.unit IS NOT NULL
                        ) units
                    ), ARRAY[]::text[]) AS units
            """
            filter_options = await conn.fetchrow(filter_options_query, tenant_id)

            # Format inventory data
            inventory_list = []
            for row in inventory_data:
                inventory_list.append({
                    "id": str(row['id']),
                    "ingredient_id": str(row['ingredient_id']),
                    "ingredient_name": row['ingredient_name'],
                    "unit": row['unit'],
                    "category": row['category'],
                    "current_stock": _json_decimal(row['current_stock'], _QUANTITY_JSON_SCALE, 0),
                    "minimum_stock": _json_decimal(row['minimum_stock'], _QUANTITY_JSON_SCALE, 0),
                    "maximum_stock": _json_decimal(row['maximum_stock'], _QUANTITY_JSON_SCALE),
                    "last_updated": row['last_updated'].isoformat() if row['last_updated'] else None,
                    "location": row['location'],
                    "lote_actual": row['lote_actual'],
                    "fecha_vencimiento": row['fecha_vencimiento'].isoformat() if row['fecha_vencimiento'] else None,
                    "status": row['status'],
                    "stock_percentage": _json_decimal(row['stock_percentage'], _PERCENT_JSON_SCALE),
                    "unit_cost": _json_decimal(row['unit_cost'], _MONEY_JSON_SCALE),
                    "total_value": _json_decimal(row['total_value'], _MONEY_JSON_SCALE, 0)
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
                    "zero_count": stats['zero_count'],
                    "low_stock_count": stats['low_stock_count'],
                    "ok_count": stats['ok_count'],
                    "total_inventory_value": _json_decimal(stats['total_inventory_value'], _MONEY_JSON_SCALE, 0)
                },
                "filter_options": {
                    "categories": list(filter_options['categories'] or []),
                    "units": list(filter_options['units'] or [])
                }
            }

    except AuthenticationError:
        raise
    except Exception as e:
        logger.error(f"Error getting inventory stock: {str(e)}")
        raise HTTPException(status_code=500, detail="Error interno del servidor")


async def get_inventory_stock(
    request: Request,
    response: Response,
    limit: int = 250,
    offset: int = 0,
    search: Optional[str] = None,
    status_filter: Optional[str] = None,  # 'low', 'critical'/'negative', 'ok', 'zero', 'all'
    category: Optional[str] = None,
    unit: Optional[str] = None,
    sort_field: str = "current_stock",
    sort_direction: str = "desc"
) -> Dict[str, Any]:
    """
    Get current inventory stock with stats for the current session tenant.
    """
    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id

    return await _get_inventory_stock_for_tenant(
        tenant_id,
        limit=limit,
        offset=offset,
        search=search,
        status_filter=status_filter,
        category=category,
        unit=unit,
        sort_field=sort_field,
        sort_direction=sort_direction
    )


async def _get_inventory_movements_for_tenant(
    tenant_id: str,
    *,
    limit: int = 100,
    offset: int = 0,
    ingredient_id: Optional[UUID] = None,
    movement_type: Optional[str] = None,  # 'purchase', 'consumption', 'adjustment', etc.
    quantity_direction: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None
) -> Dict[str, Any]:
    """
    Get inventory movements history for a tenant.

    Args:
        tenant_id: Tenant to query
        limit: Number of records to return
        offset: Number of records to skip
        ingredient_id: Filter by specific ingredient
        movement_type: Filter by movement type
        quantity_direction: Filter by quantity sign ('positive' or 'negative')
        start_date: Filter by start date
        end_date: Filter by end date

    Returns:
        Dictionary with movements data
    """
    try:
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

            if quantity_direction == "positive":
                base_query += " AND tim.quantity_change >= 0"
                count_query += " AND tim.quantity_change >= 0"
            elif quantity_direction == "negative":
                base_query += " AND tim.quantity_change < 0"
                count_query += " AND tim.quantity_change < 0"

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
                    "quantity_change": _json_decimal(row['quantity_change'], _QUANTITY_JSON_SCALE, 0),
                    "previous_stock": _json_decimal(row['previous_stock'], _QUANTITY_JSON_SCALE, 0),
                    "new_stock": _json_decimal(row['new_stock'], _QUANTITY_JSON_SCALE, 0),
                    "cost_per_unit": _json_decimal(row['cost_per_unit'], _MONEY_JSON_SCALE),
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


async def get_inventory_movements(
    request: Request,
    response: Response,
    limit: int = 100,
    offset: int = 0,
    ingredient_id: Optional[UUID] = None,
    movement_type: Optional[str] = None,  # 'purchase', 'consumption', 'adjustment', etc.
    quantity_direction: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None
) -> Dict[str, Any]:
    """
    Get inventory movements history for the current session tenant.
    """
    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id

    return await _get_inventory_movements_for_tenant(
        tenant_id,
        limit=limit,
        offset=offset,
        ingredient_id=ingredient_id,
        movement_type=movement_type,
        quantity_direction=quantity_direction,
        start_date=start_date,
        end_date=end_date
    )


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
                "current_stock": _json_decimal(stock_data['current_stock'], _QUANTITY_JSON_SCALE, 0),
                "minimum_stock": _json_decimal(stock_data['minimum_stock'], _QUANTITY_JSON_SCALE, 0),
                "maximum_stock": _json_decimal(stock_data['maximum_stock'], _QUANTITY_JSON_SCALE),
                "last_updated": stock_data['last_updated'].isoformat() if stock_data['last_updated'] else None,
                "location": stock_data['location'],
                "lote_actual": stock_data['lote_actual'],
                "fecha_vencimiento": stock_data['fecha_vencimiento'].isoformat() if stock_data['fecha_vencimiento'] else None,
                "unit_cost": _json_decimal(stock_data['unit_cost'], _TECHNICAL_COST_JSON_SCALE)
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
        quantity_change = _decimal_value(body.get('quantity_change'))
        reason = body.get('reason', 'Manual adjustment')
        source = body.get('source', 'manual_adjustment')
        # New fields for enhanced adjustments
        purchase_unit = body.get('unit')  # Optional: unit selected by user
        cost_per_unit = body.get('cost_per_unit')  # Optional: cost per unit (only for positive adjustments)

        if not ingredient_id:
            raise HTTPException(status_code=400, detail="ingredient_id is required")

        async with get_db_connection() as conn:
            # Start transaction
            async with conn.transaction():
                # Get ingredient base unit
                ingredient_query = """
                    SELECT unit
                    FROM ingredients
                    WHERE id = $1
                """
                ingredient_row = await conn.fetchrow(ingredient_query, ingredient_id)

                if not ingredient_row:
                    raise HTTPException(status_code=404, detail="Ingrediente no encontrado")

                base_unit = ingredient_row['unit']

                # If a purchase unit is provided, get conversion factor
                conversion_factor = Decimal("1")
                unit_for_movement = base_unit

                if purchase_unit and purchase_unit != base_unit:
                    conversion_query = """
                        SELECT conversion_factor, purchase_unit
                        FROM ingredient_purchase_units
                        WHERE ingredient_id = $1 AND purchase_unit = $2 AND is_active = TRUE
                    """
                    conversion_row = await conn.fetchrow(conversion_query, ingredient_id, purchase_unit)

                    if conversion_row:
                        conversion_factor = _decimal_value(conversion_row['conversion_factor'])
                        unit_for_movement = conversion_row['purchase_unit']
                    else:
                        # If no conversion found, use base unit
                        logger.warning(f"No conversion found for {purchase_unit}, using base unit {base_unit}")
                        unit_for_movement = base_unit

                # Convert quantity to base unit
                quantity_in_base_unit = (quantity_change * conversion_factor).quantize(_QUANTITY_JSON_SCALE)

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
                    previous_stock = Decimal("0")
                else:
                    previous_stock = _decimal_value(stock_row['current_stock'])

                # Idempotent: "set to current stock" (e.g. confirm 0 when already 0).
                if quantity_change == 0:
                    return {
                        "success": True,
                        "message": "El stock ya coincide con el valor indicado",
                        "data": {
                            "ingredient_id": str(ingredient_id),
                            "quantity_change": 0,
                            "previous_stock": _json_decimal(previous_stock, _QUANTITY_JSON_SCALE, 0),
                            "new_stock": _json_decimal(previous_stock, _QUANTITY_JSON_SCALE, 0),
                            "reason": reason,
                            "created_at": None,
                        },
                    }

                # Calculate new stock (using converted quantity)
                new_stock = max(Decimal("0"), previous_stock + quantity_in_base_unit).quantize(_QUANTITY_JSON_SCALE)

                # Update stock
                update_query = """
                    UPDATE tenant_inventory
                    SET current_stock = $1, last_updated = NOW()
                    WHERE tenant_id = $2 AND ingredient_id = $3
                """
                await conn.execute(update_query, new_stock, tenant_id, ingredient_id)

                # Convert cost to base unit if provided
                cost_in_base_unit = None
                if cost_per_unit:
                    # If cost is provided with a purchase unit, convert it to base unit cost
                    # Example: $30,000 per kg → $30 per gr (30000 / 1000)
                    cost_decimal = _decimal_value(cost_per_unit)
                    cost_in_base_unit = (
                        cost_decimal / conversion_factor
                        if conversion_factor > 0
                        else cost_decimal
                    ).quantize(_TECHNICAL_COST_JSON_SCALE)

                # Create movement record with optional cost
                movement_query = """
                    INSERT INTO tenant_ingredient_movements (
                        tenant_id,
                        ingredient_id,
                        movement_type,
                        quantity_change,
                        unit,
                        previous_stock,
                        new_stock,
                        cost_per_unit,
                        reference_table,
                        reason,
                        created_by,
                        created_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW())
                    RETURNING id, created_at
                """
                movement_row = await conn.fetchrow(
                    movement_query,
                    tenant_id,
                    ingredient_id,
                    'adjustment',
                    quantity_in_base_unit,  # Use converted quantity
                    base_unit,  # Always use base unit for storage
                    previous_stock,
                    new_stock,
                    cost_in_base_unit,  # Use converted cost in base unit
                    source,
                    reason,
                    user_id
                )

                logger.info(
                    f"Adjustment created: {ingredient_id} - "
                    f"{quantity_change}{unit_for_movement} ({quantity_in_base_unit}{base_unit}) "
                    f"@ ${cost_per_unit}/{unit_for_movement if cost_per_unit else 'N/A'}"
                )

                return {
                    "success": True,
                    "message": "Ajuste de inventario creado exitosamente",
                    "data": {
                        "id": str(movement_row['id']),
                        "ingredient_id": str(ingredient_id),
                        "quantity_change": _json_decimal(quantity_in_base_unit, _QUANTITY_JSON_SCALE, 0),
                        "previous_stock": _json_decimal(previous_stock, _QUANTITY_JSON_SCALE, 0),
                        "new_stock": _json_decimal(new_stock, _QUANTITY_JSON_SCALE, 0),
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

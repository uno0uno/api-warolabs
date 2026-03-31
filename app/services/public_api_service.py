"""
Public API Service
Handles data access for external integrations via API tokens
"""
from typing import Optional
from uuid import UUID
from fastapi import Request
from app.database import get_db_connection
from app.core.middleware import get_api_key_context, get_tenant_context
from app.core.exceptions import AuthenticationError, AuthorizationError, APIError
from datetime import datetime, date
import logging

logger = logging.getLogger(__name__)


def parse_date(date_str: Optional[str]) -> Optional[date]:
    """Convert date string (YYYY-MM-DD) to date object"""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return None


def validate_api_key_auth(request: Request, required_scope: str) -> tuple[str, str]:
    """
    Validate API key authentication and scope.
    Returns (tenant_id, token_id) if valid.
    Raises AuthenticationError or AuthorizationError if invalid.
    """
    api_key_context = get_api_key_context(request)

    if not api_key_context.is_valid:
        raise AuthenticationError("API key requerida. Usa el header 'Authorization: Bearer waro_sk_xxx' o 'X-API-Key: waro_sk_xxx'")

    if not api_key_context.has_scope(required_scope):
        raise AuthorizationError(f"API key no tiene el permiso requerido: {required_scope}")

    return api_key_context.tenant_id, api_key_context.token_id


async def get_sales_list(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    payment_method: Optional[str] = None,
    status: Optional[str] = None,
    sort_field: str = "order_date",
    sort_direction: str = "desc",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    timezone: str = "America/Bogota"
) -> dict:
    """
    Get list of sales (orders) for the authenticated tenant via API key.
    Requires scope: orders:read or read
    """
    try:
        tenant_id, token_id = validate_api_key_auth(request, "orders:read")

        async with get_db_connection(use_transaction=False) as conn:
            # Build WHERE clause
            where_conditions = ["o.tenant_id = $1", "o.pos_cart_id IS NOT NULL"]
            params = [UUID(tenant_id)]
            param_count = 1

            # Payment method filter
            if payment_method:
                param_count += 1
                where_conditions.append(f"o.payment_method = ${param_count}")
                params.append(payment_method)

            # Status filter
            if status:
                param_count += 1
                where_conditions.append(f"o.status = ${param_count}")
                params.append(status)

            # Date range filter
            parsed_date_from = parse_date(date_from)
            parsed_date_to = parse_date(date_to)

            if parsed_date_from:
                param_count += 1
                date_from_param = param_count
                param_count += 1
                tz_from_param = param_count
                where_conditions.append(f"o.order_date >= (${date_from_param}::timestamp AT TIME ZONE ${tz_from_param})")
                params.append(parsed_date_from)
                params.append(timezone)

            if parsed_date_to:
                param_count += 1
                date_to_param = param_count
                param_count += 1
                tz_to_param = param_count
                where_conditions.append(f"o.order_date < ((${date_to_param}::timestamp + interval '1 day') AT TIME ZONE ${tz_to_param})")
                params.append(parsed_date_to)
                params.append(timezone)

            where_clause = " AND ".join(where_conditions)

            # Validate sort field
            allowed_sort_fields = ["order_number", "order_date", "total_amount", "customer_name", "payment_method"]
            if sort_field not in allowed_sort_fields:
                sort_field = "order_date"

            sort_direction = "ASC" if sort_direction.lower() == "asc" else "DESC"

            # Map sort field to actual column
            sort_column_map = {
                "order_number": "o.order_number",
                "order_date": "o.order_date",
                "total_amount": "o.total_amount",
                "customer_name": "p.name",
                "payment_method": "o.payment_method"
            }
            sort_column = sort_column_map.get(sort_field, "o.order_date")

            # Get total count
            count_query = f"""
                SELECT COUNT(*) as total
                FROM orders o
                LEFT JOIN profile p ON o.customer_id = p.id
                WHERE {where_clause}
            """
            count_row = await conn.fetchrow(count_query, *params)
            total_count = count_row['total']

            # Get orders with items
            param_count += 1
            limit_param = param_count
            param_count += 1
            offset_param = param_count

            orders_query = f"""
                SELECT
                    o.id,
                    o.order_number,
                    o.order_date,
                    o.total_amount,
                    o.status,
                    o.payment_method,
                    p.id as customer_id,
                    p.name as customer_name,
                    p.phone_number as customer_phone,
                    COALESCE(
                        json_agg(
                            json_build_object(
                                'id', oi.id,
                                'quantity', oi.quantity,
                                'price', oi.price_at_purchase,
                                'subtotal', oi.subtotal,
                                'product', json_build_object(
                                    'id', prod.id,
                                    'name', prod.name
                                )
                            )
                        ) FILTER (WHERE oi.id IS NOT NULL),
                        '[]'
                    ) as items
                FROM orders o
                LEFT JOIN profile p ON o.customer_id = p.id
                LEFT JOIN order_items oi ON o.id = oi.order_id
                LEFT JOIN product prod ON oi.product_id = prod.id
                WHERE {where_clause}
                GROUP BY o.id, p.id
                ORDER BY {sort_column} {sort_direction}
                LIMIT ${limit_param} OFFSET ${offset_param}
            """

            params.extend([limit, offset])
            orders_rows = await conn.fetch(orders_query, *params)

            sales = []
            for row in orders_rows:
                # Parse items JSON if it's a string (depends on driver/DB version sometimes, but usually asyncpg returns list/dict for json)
                # asyncpg usually decodes json automatically if the column type is json/jsonb.
                # json_agg returns json type.
                items_data = row['items']
                if isinstance(items_data, str):
                    import json
                    items_data = json.loads(items_data)
                
                sales.append({
                    "id": str(row['id']),
                    "orderNumber": int(row['order_number']),
                    "orderDate": row['order_date'].isoformat(),
                    "totalAmount": float(row['total_amount']),
                    "status": row['status'],
                    "paymentMethod": row['payment_method'],
                    "customer": {
                        "id": str(row['customer_id']) if row['customer_id'] else None,
                        "name": row['customer_name'],
                        "phone": row['customer_phone']
                    },
                    "items": items_data,
                    "itemsCount": len(items_data)
                })

            return {
                "success": True,
                "data": sales,
                "pagination": {
                    "total": total_count,
                    "limit": limit,
                    "offset": offset,
                    "hasMore": (offset + limit) < total_count
                },
                "meta": {
                    "tokenId": token_id,
                    "tenantId": tenant_id,
                    "timezone": timezone
                }
            }

    except (AuthenticationError, AuthorizationError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting sales list via API: {str(e)}")
        raise APIError(f"Error al obtener ventas: {str(e)}", status_code=500)


async def get_sale_by_id(
    request: Request,
    order_id: UUID
) -> dict:
    """
    Get single sale (order) by ID for the authenticated tenant via API key.
    Requires scope: orders:read or read
    """
    try:
        tenant_id, token_id = validate_api_key_auth(request, "orders:read")

        async with get_db_connection(use_transaction=False) as conn:
            order_query = """
                SELECT
                    o.id,
                    o.order_number,
                    o.order_date,
                    o.total_amount,
                    o.status,
                    o.payment_method,
                    p.id as customer_id,
                    p.name as customer_name,
                    p.phone_number as customer_phone
                FROM orders o
                LEFT JOIN profile p ON o.customer_id = p.id
                WHERE o.id = $1 AND o.tenant_id = $2 AND o.pos_cart_id IS NOT NULL
            """

            order_row = await conn.fetchrow(order_query, order_id, UUID(tenant_id))

            if not order_row:
                raise APIError("Venta no encontrada", status_code=404)

            # Get order items
            items_query = """
                SELECT
                    oi.id,
                    oi.quantity,
                    oi.price_at_purchase,
                    oi.subtotal,
                    p.id as product_id,
                    p.name as product_name
                FROM order_items oi
                LEFT JOIN product p ON oi.product_id = p.id
                WHERE oi.order_id = $1
                ORDER BY oi.created_at
            """
            items_rows = await conn.fetch(items_query, order_id)

            items = []
            for item_row in items_rows:
                # Get modifiers for this item
                modifiers_query = """
                    SELECT
                        modifier_name,
                        price_at_purchase,
                        quantity
                    FROM order_item_modifiers
                    WHERE order_item_id = $1
                """
                modifiers_rows = await conn.fetch(modifiers_query, item_row['id'])

                modifiers = [
                    {
                        "name": mod['modifier_name'],
                        "price": float(mod['price_at_purchase']),
                        "quantity": float(mod['quantity']) if mod['quantity'] else 1
                    }
                    for mod in modifiers_rows
                ]

                items.append({
                    "id": str(item_row['id']),
                    "quantity": float(item_row['quantity']),
                    "priceAtPurchase": float(item_row['price_at_purchase']),
                    "subtotal": float(item_row['subtotal']),
                    "product": {
                        "id": str(item_row['product_id']) if item_row['product_id'] else None,
                        "name": item_row['product_name']
                    },
                    "modifiers": modifiers
                })

            return {
                "success": True,
                "data": {
                    "id": str(order_row['id']),
                    "orderNumber": int(order_row['order_number']),
                    "orderDate": order_row['order_date'].isoformat(),
                    "totalAmount": float(order_row['total_amount']),
                    "status": order_row['status'],
                    "paymentMethod": order_row['payment_method'],
                    "customer": {
                        "id": str(order_row['customer_id']) if order_row['customer_id'] else None,
                        "name": order_row['customer_name'],
                        "phone": order_row['customer_phone']
                    },
                    "items": items
                },
                "meta": {
                    "tokenId": token_id,
                    "tenantId": tenant_id
                }
            }

    except (AuthenticationError, AuthorizationError, APIError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting sale by ID via API: {str(e)}")
        raise APIError(f"Error al obtener venta: {str(e)}", status_code=500)


async def get_sales_metrics(
    request: Request,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    timezone: str = "America/Bogota",
    group_by: Optional[str] = None,  # date, weekday, hour, product, payment, ticket
    limit: int = 20,  # for product grouping
    sort_by: str = "quantity",  # for product grouping: quantity, revenue
    ranges: Optional[list] = None  # for ticket grouping
) -> dict:
    """
    Get sales metrics for the authenticated tenant via API key.

    groupBy options:
    - None: Overall metrics (default)
    - "date": Metrics grouped by specific date (day by day)
    - "weekday": Metrics grouped by day of week (aggregated)
    - "hour": Metrics grouped by hour of day
    - "product": Top selling products
    - "payment": Breakdown by payment method
    - "ticket": Distribution by ticket price ranges

    Requires scope: orders:read or read
    """
    try:
        tenant_id, token_id = validate_api_key_auth(request, "orders:read")

        async with get_db_connection(use_transaction=False) as conn:
            # Build base WHERE clause
            where_conditions = ["tenant_id = $1", "pos_cart_id IS NOT NULL", "status = 'completed'"]
            params = [UUID(tenant_id)]
            param_count = 1

            parsed_date_from = parse_date(date_from)
            parsed_date_to = parse_date(date_to)

            if parsed_date_from:
                param_count += 1
                date_from_param = param_count
                param_count += 1
                tz_from_param = param_count
                where_conditions.append(f"order_date >= (${date_from_param}::timestamp AT TIME ZONE ${tz_from_param})")
                params.append(parsed_date_from)
                params.append(timezone)

            if parsed_date_to:
                param_count += 1
                date_to_param = param_count
                param_count += 1
                tz_to_param = param_count
                where_conditions.append(f"order_date < ((${date_to_param}::timestamp + interval '1 day') AT TIME ZONE ${tz_to_param})")
                params.append(parsed_date_to)
                params.append(timezone)

            where_clause = " AND ".join(where_conditions)

            meta = {
                "tokenId": token_id,
                "tenantId": tenant_id,
                "dateFrom": date_from,
                "dateTo": date_to,
                "timezone": timezone
            }

            # Route to appropriate grouping
            if group_by == "date":
                return await _metrics_by_date(conn, where_clause, params, param_count, timezone, meta)
            elif group_by == "weekday":
                return await _metrics_by_weekday(conn, where_clause, params, param_count, timezone, meta)
            elif group_by == "hour":
                return await _metrics_by_hour(conn, where_clause, params, param_count, timezone, meta)
            elif group_by == "product":
                meta["sortBy"] = sort_by
                meta["limit"] = limit
                return await _metrics_by_product(conn, where_clause, params, param_count, timezone, limit, sort_by, meta)
            elif group_by == "payment":
                return await _metrics_by_payment(conn, where_clause, params, meta)
            elif group_by == "ticket":
                return await _metrics_by_ticket(conn, where_clause, params, timezone, ranges, meta)
            else:
                # Default: overall metrics (include all statuses for this)
                where_conditions_all = ["tenant_id = $1", "pos_cart_id IS NOT NULL"]
                params_all = [UUID(tenant_id)]
                pc = 1

                if parsed_date_from:
                    pc += 1
                    pc += 1
                    where_conditions_all.append(f"order_date >= (${pc-1}::timestamp AT TIME ZONE ${pc})")
                    params_all.append(parsed_date_from)
                    params_all.append(timezone)

                if parsed_date_to:
                    pc += 1
                    pc += 1
                    where_conditions_all.append(f"order_date < ((${pc-1}::timestamp + interval '1 day') AT TIME ZONE ${pc})")
                    params_all.append(parsed_date_to)
                    params_all.append(timezone)

                where_clause_all = " AND ".join(where_conditions_all)

                metrics_query = f"""
                    SELECT
                        COUNT(*) as total_orders,
                        COUNT(*) FILTER (WHERE status = 'completed') as completed_orders,
                        COUNT(*) FILTER (WHERE status = 'cancelled') as cancelled_orders,
                        COUNT(*) FILTER (WHERE status = 'pending') as pending_orders,
                        COALESCE(SUM(total_amount) FILTER (WHERE status = 'completed'), 0) as total_sales,
                        COALESCE(AVG(total_amount) FILTER (WHERE status = 'completed'), 0) as avg_ticket
                    FROM orders
                    WHERE {where_clause_all}
                """

                row = await conn.fetchrow(metrics_query, *params_all)

                return {
                    "success": True,
                    "data": {
                        "totalSales": float(row['total_sales']),
                        "totalOrders": row['total_orders'],
                        "completedOrders": row['completed_orders'],
                        "cancelledOrders": row['cancelled_orders'],
                        "pendingOrders": row['pending_orders'],
                        "avgTicket": float(row['avg_ticket'])
                    },
                    "meta": meta
                }

    except (AuthenticationError, AuthorizationError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting sales metrics via API: {str(e)}")
        raise APIError(f"Error al obtener metricas: {str(e)}", status_code=500)


async def _metrics_by_date(conn, where_clause, params, param_count, timezone, meta):
    """Internal: Get metrics grouped by specific date"""
    param_count += 1
    tz_query_param = param_count
    params.append(timezone)

    query = f"""
        SELECT
            DATE(order_date AT TIME ZONE ${tz_query_param}) as order_day,
            TO_CHAR(order_date AT TIME ZONE ${tz_query_param}, 'Day') as day_name,
            COUNT(*) as total_orders,
            COALESCE(SUM(total_amount), 0) as total_sales,
            COALESCE(AVG(total_amount), 0) as avg_ticket
        FROM orders
        WHERE {where_clause}
        GROUP BY DATE(order_date AT TIME ZONE ${tz_query_param}),
                 TO_CHAR(order_date AT TIME ZONE ${tz_query_param}, 'Day')
        ORDER BY order_day
    """

    rows = await conn.fetch(query, *params)

    data = [{
        "date": row['order_day'].isoformat(),
        "dayName": row['day_name'].strip(),
        "totalOrders": row['total_orders'],
        "totalSales": float(row['total_sales']),
        "avgTicket": float(row['avg_ticket'])
    } for row in rows]

    meta["groupBy"] = "date"
    return {"success": True, "data": data, "meta": meta}


async def _metrics_by_weekday(conn, where_clause, params, param_count, timezone, meta):
    """Internal: Get metrics grouped by weekday with day count and averages"""
    param_count += 1
    tz_query_param = param_count
    params.append(timezone)

    query = f"""
        SELECT
            EXTRACT(DOW FROM order_date AT TIME ZONE ${tz_query_param}) as day_number,
            COUNT(*) as total_orders,
            COALESCE(SUM(total_amount), 0) as total_sales,
            COALESCE(AVG(total_amount), 0) as avg_ticket,
            COUNT(DISTINCT DATE(order_date AT TIME ZONE ${tz_query_param})) as day_count
        FROM orders
        WHERE {where_clause}
        GROUP BY EXTRACT(DOW FROM order_date AT TIME ZONE ${tz_query_param})
        ORDER BY day_number
    """

    rows = await conn.fetch(query, *params)
    days_map = {0: "Sunday", 1: "Monday", 2: "Tuesday", 3: "Wednesday",
               4: "Thursday", 5: "Friday", 6: "Saturday"}

    data = []
    for row in rows:
        day_count = row['day_count']
        total_orders = row['total_orders']
        total_sales = float(row['total_sales'])
        data.append({
            "dayNumber": int(row['day_number']),
            "dayName": days_map.get(int(row['day_number']), ""),
            "dayCount": day_count,
            "totalOrders": total_orders,
            "totalSales": total_sales,
            "avgTicket": float(row['avg_ticket']),
            "avgOrdersPerDay": round(total_orders / day_count, 1) if day_count > 0 else 0,
            "avgSalesPerDay": round(total_sales / day_count, 0) if day_count > 0 else 0
        })

    meta["groupBy"] = "weekday"
    return {"success": True, "data": data, "meta": meta}


async def _metrics_by_hour(conn, where_clause, params, param_count, timezone, meta):
    """Internal: Get metrics grouped by hour with day count and averages"""
    param_count += 1
    tz_query_param = param_count
    params.append(timezone)

    query = f"""
        SELECT
            EXTRACT(HOUR FROM order_date AT TIME ZONE ${tz_query_param}) as hour,
            COUNT(*) as total_orders,
            COALESCE(SUM(total_amount), 0) as total_sales,
            COALESCE(AVG(total_amount), 0) as avg_ticket,
            COUNT(DISTINCT DATE(order_date AT TIME ZONE ${tz_query_param})) as day_count
        FROM orders
        WHERE {where_clause}
        GROUP BY EXTRACT(HOUR FROM order_date AT TIME ZONE ${tz_query_param})
        ORDER BY hour
    """

    rows = await conn.fetch(query, *params)

    data = []
    for row in rows:
        day_count = row['day_count']
        total_orders = row['total_orders']
        total_sales = float(row['total_sales'])
        data.append({
            "hour": int(row['hour']),
            "hourLabel": f"{int(row['hour']):02d}:00",
            "dayCount": day_count,
            "totalOrders": total_orders,
            "totalSales": total_sales,
            "avgTicket": float(row['avg_ticket']),
            "avgOrdersPerDay": round(total_orders / day_count, 1) if day_count > 0 else 0,
            "avgSalesPerDay": round(total_sales / day_count, 0) if day_count > 0 else 0
        })

    meta["groupBy"] = "hour"
    return {"success": True, "data": data, "meta": meta}


async def _metrics_by_product(conn, where_clause, params, param_count, timezone, limit, sort_by, meta):
    """Internal: Get top products"""
    # Need to modify where_clause for JOINs
    where_clause_products = where_clause.replace("tenant_id", "o.tenant_id").replace("pos_cart_id", "o.pos_cart_id").replace("status", "o.status").replace("order_date", "o.order_date")

    sort_column = "total_quantity" if sort_by == "quantity" else "total_revenue"

    param_count += 1
    limit_param = param_count
    params.append(limit)

    query = f"""
        SELECT
            p.id as product_id,
            p.name as product_name,
            c.name as category_name,
            SUM(oi.quantity) as total_quantity,
            SUM(oi.subtotal) as total_revenue,
            COUNT(DISTINCT o.id) as orders_count,
            AVG(oi.price_at_purchase) as avg_price
        FROM order_items oi
        JOIN orders o ON oi.order_id = o.id
        JOIN product p ON oi.product_id = p.id
        LEFT JOIN categories c ON p.category_id = c.id
        WHERE {where_clause_products}
        GROUP BY p.id, p.name, c.name
        ORDER BY {sort_column} DESC
        LIMIT ${limit_param}
    """

    rows = await conn.fetch(query, *params)

    data = [{
        "rank": idx,
        "productId": str(row['product_id']),
        "productName": row['product_name'],
        "categoryName": row['category_name'],
        "totalQuantity": float(row['total_quantity']),
        "totalRevenue": float(row['total_revenue']),
        "ordersCount": row['orders_count'],
        "avgPrice": float(row['avg_price'])
    } for idx, row in enumerate(rows, 1)]

    meta["groupBy"] = "product"
    return {"success": True, "data": data, "meta": meta}


async def _metrics_by_payment(conn, where_clause, params, meta):
    """Internal: Get breakdown by payment method"""
    query = f"""
        SELECT
            payment_method,
            COUNT(*) as orders_count,
            COALESCE(SUM(total_amount), 0) as total_sales,
            COALESCE(AVG(total_amount), 0) as avg_ticket
        FROM orders
        WHERE {where_clause}
        GROUP BY payment_method
        ORDER BY total_sales DESC
    """

    rows = await conn.fetch(query, *params)

    total_orders = sum(row['orders_count'] for row in rows)
    total_sales = sum(float(row['total_sales']) for row in rows)

    data = [{
        "paymentMethod": row['payment_method'],
        "ordersCount": row['orders_count'],
        "ordersPercentage": round((row['orders_count'] / total_orders * 100), 1) if total_orders > 0 else 0,
        "totalSales": float(row['total_sales']),
        "salesPercentage": round((float(row['total_sales']) / total_sales * 100), 1) if total_sales > 0 else 0,
        "avgTicket": float(row['avg_ticket'])
    } for row in rows]

    meta["groupBy"] = "payment"
    meta["totalOrders"] = total_orders
    meta["totalSales"] = total_sales
    return {"success": True, "data": data, "meta": meta}


async def _metrics_by_ticket(conn, where_clause, params, timezone, ranges, meta):
    """Internal: Get ticket distribution"""
    if not ranges:
        ranges = [0, 15000, 25000, 40000, 60000, 100000, 999999999]

    case_parts = []
    for i in range(len(ranges) - 1):
        low, high = ranges[i], ranges[i + 1]
        label = f"${low:,}+" if high == 999999999 else f"${low:,} - ${high:,}"
        case_parts.append(f"WHEN total_amount >= {low} AND total_amount < {high} THEN '{label}'")

    case_statement = "CASE " + " ".join(case_parts) + " END"

    query = f"""
        SELECT
            {case_statement} as range_label,
            COUNT(*) as orders_count,
            COALESCE(SUM(total_amount), 0) as total_sales,
            COALESCE(AVG(total_amount), 0) as avg_ticket
        FROM orders
        WHERE {where_clause}
        GROUP BY {case_statement}
        ORDER BY MIN(total_amount)
    """

    rows = await conn.fetch(query, *params)
    total_orders = sum(row['orders_count'] for row in rows)

    data = [{
        "range": row['range_label'],
        "ordersCount": row['orders_count'],
        "percentage": round((row['orders_count'] / total_orders * 100), 1) if total_orders > 0 else 0,
        "totalSales": float(row['total_sales']),
        "avgTicket": float(row['avg_ticket'])
    } for row in rows if row['range_label']]

    meta["groupBy"] = "ticket"
    meta["totalOrders"] = total_orders
    return {"success": True, "data": data, "meta": meta}


async def get_menu_products(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    category_id: Optional[str] = None,
    is_available: Optional[bool] = None,
    include_ingredients: bool = True,
    include_recipe_bases: bool = True,
    include_modifiers: bool = True
) -> dict:
    """
    Get list of menu products with ingredients, recipe bases and modifiers.
    Requires scope: menu:read or read
    """
    try:
        tenant_id, token_id = validate_api_key_auth(request, "menu:read")

        async with get_db_connection(use_transaction=False) as conn:
            # Build WHERE clause
            where_conditions = ["p.tenant_id = $1"]
            params = [UUID(tenant_id)]
            param_count = 1

            if category_id:
                param_count += 1
                where_conditions.append(f"p.category_id = ${param_count}")
                params.append(UUID(category_id))

            if is_available is not None:
                param_count += 1
                where_conditions.append(f"p.is_available = ${param_count}")
                params.append(is_available)

            where_clause = " AND ".join(where_conditions)

            # Get total count
            count_query = f"SELECT COUNT(*) as total FROM product p WHERE {where_clause}"
            count_row = await conn.fetchrow(count_query, *params)
            total_count = count_row['total']

            # Get products
            param_count += 1
            limit_param = param_count
            param_count += 1
            offset_param = param_count

            products_query = f"""
                SELECT
                    p.id,
                    p.name,
                    p.description,
                    p.price,
                    p.is_available,
                    p.allow_modifiers,
                    p.preparation_time,
                    p.costo_calculado,
                    p.category_id,
                    c.name as category_name
                FROM product p
                LEFT JOIN categories c ON p.category_id = c.id
                WHERE {where_clause}
                ORDER BY p.name ASC
                LIMIT ${limit_param} OFFSET ${offset_param}
            """
            params.extend([limit, offset])
            products_rows = await conn.fetch(products_query, *params)

            products = []
            for row in products_rows:
                product = {
                    "id": str(row['id']),
                    "name": row['name'],
                    "description": row['description'],
                    "price": float(row['price']) if row['price'] else 0,
                    "isAvailable": row['is_available'],
                    "allowModifiers": row['allow_modifiers'],
                    "preparationTime": row['preparation_time'],
                    "calculatedCost": float(row['costo_calculado']) if row['costo_calculado'] else None,
                    "category": {
                        "id": str(row['category_id']) if row['category_id'] else None,
                        "name": row['category_name']
                    } if row['category_id'] else None
                }

                # Get direct ingredients
                if include_ingredients:
                    ingredients_query = """
                        SELECT
                            pr.ingredient_id,
                            pr.quantity,
                            pr.unit,
                            i.name as ingredient_name
                        FROM product_recipes pr
                        JOIN ingredients i ON pr.ingredient_id = i.id
                        WHERE pr.product_id = $1
                    """
                    ingredients_rows = await conn.fetch(ingredients_query, row['id'])
                    product["ingredients"] = [
                        {
                            "id": str(ing['ingredient_id']),
                            "name": ing['ingredient_name'],
                            "quantity": float(ing['quantity']),
                            "unit": ing['unit']
                        }
                        for ing in ingredients_rows
                    ]

                # Get recipe bases with their ingredients
                if include_recipe_bases:
                    recipe_bases_query = """
                        SELECT
                            pbt.id,
                            pbt.name,
                            pbt.description
                        FROM product_base_recipes pbr
                        JOIN product_base_types pbt ON pbr.product_base_type_id = pbt.id
                        WHERE pbr.product_id = $1
                    """
                    recipe_bases_rows = await conn.fetch(recipe_bases_query, row['id'])

                    recipe_bases = []
                    for rb in recipe_bases_rows:
                        # Get ingredients for this recipe base
                        rb_ingredients_query = """
                            SELECT
                                brt.ingredient_id,
                                brt.base_quantity,
                                brt.unit,
                                brt.is_required,
                                i.name as ingredient_name
                            FROM base_recipe_templates brt
                            JOIN ingredients i ON brt.ingredient_id = i.id
                            WHERE brt.product_base_type_id = $1
                        """
                        rb_ingredients_rows = await conn.fetch(rb_ingredients_query, rb['id'])

                        recipe_bases.append({
                            "id": str(rb['id']),
                            "name": rb['name'],
                            "description": rb['description'],
                            "ingredients": [
                                {
                                    "id": str(rbi['ingredient_id']),
                                    "name": rbi['ingredient_name'],
                                    "quantity": float(rbi['base_quantity']),
                                    "unit": rbi['unit'],
                                    "isRequired": rbi['is_required']
                                }
                                for rbi in rb_ingredients_rows
                            ]
                        })
                    product["recipeBases"] = recipe_bases

                # Get modifier groups
                if include_modifiers:
                    modifiers_query = """
                        SELECT
                            mg.id,
                            mg.name,
                            mg.min_qty,
                            mg.max_qty,
                            mg.is_required
                        FROM product_modifier_groups pmg
                        JOIN modifier_groups mg ON pmg.modifier_group_id = mg.id
                        WHERE pmg.product_id = $1
                    """
                    modifier_groups_rows = await conn.fetch(modifiers_query, row['id'])

                    modifier_groups = []
                    for mg in modifier_groups_rows:
                        # Get modifiers for this group
                        options_query = """
                            SELECT
                                m.id,
                                m.name,
                                m.price,
                                m.is_available,
                                m.ingredient_id,
                                m.ingredient_quantity,
                                m.ingredient_unit,
                                i.name as ingredient_name
                            FROM modifiers m
                            LEFT JOIN ingredients i ON m.ingredient_id = i.id
                            WHERE m.modifier_group_id = $1
                            ORDER BY m.sort_order
                        """
                        options_rows = await conn.fetch(options_query, mg['id'])

                        modifier_groups.append({
                            "id": str(mg['id']),
                            "name": mg['name'],
                            "minQty": mg['min_qty'],
                            "maxQty": mg['max_qty'],
                            "isRequired": mg['is_required'],
                            "options": [
                                {
                                    "id": str(opt['id']),
                                    "name": opt['name'],
                                    "price": float(opt['price']) if opt['price'] else 0,
                                    "isAvailable": opt['is_available'],
                                    "ingredient": {
                                        "id": str(opt['ingredient_id']),
                                        "name": opt['ingredient_name'],
                                        "quantity": float(opt['ingredient_quantity']) if opt['ingredient_quantity'] else None,
                                        "unit": opt['ingredient_unit']
                                    } if opt['ingredient_id'] else None
                                }
                                for opt in options_rows
                            ]
                        })
                    product["modifierGroups"] = modifier_groups

                products.append(product)

            return {
                "success": True,
                "data": products,
                "pagination": {
                    "total": total_count,
                    "limit": limit,
                    "offset": offset,
                    "hasMore": (offset + limit) < total_count
                },
                "meta": {
                    "tokenId": token_id,
                    "tenantId": tenant_id
                }
            }

    except (AuthenticationError, AuthorizationError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting menu products via API: {str(e)}")
        raise APIError(f"Error al obtener productos: {str(e)}", status_code=500)


async def get_menu_recipes(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    is_active: Optional[bool] = None
) -> dict:
    """
    Get list of recipe bases with ingredients.
    Requires scope: menu:read or read
    """
    try:
        tenant_id, token_id = validate_api_key_auth(request, "menu:read")

        async with get_db_connection(use_transaction=False) as conn:
            # Build WHERE clause
            where_conditions = ["pbt.tenant_id = $1"]
            params = [UUID(tenant_id)]
            param_count = 1

            if is_active is not None:
                param_count += 1
                where_conditions.append(f"pbt.is_active = ${param_count}")
                params.append(is_active)

            where_clause = " AND ".join(where_conditions)

            # Get total count
            count_query = f"SELECT COUNT(*) as total FROM product_base_types pbt WHERE {where_clause}"
            count_row = await conn.fetchrow(count_query, *params)
            total_count = count_row['total']

            # Get recipe bases
            param_count += 1
            limit_param = param_count
            param_count += 1
            offset_param = param_count

            recipes_query = f"""
                SELECT
                    pbt.id,
                    pbt.name,
                    pbt.description,
                    pbt.is_active,
                    pbt.created_at,
                    pbt.updated_at
                FROM product_base_types pbt
                WHERE {where_clause}
                ORDER BY pbt.name ASC
                LIMIT ${limit_param} OFFSET ${offset_param}
            """
            params.extend([limit, offset])
            recipes_rows = await conn.fetch(recipes_query, *params)

            recipes = []
            for row in recipes_rows:
                # Get ingredients for this recipe base
                ingredients_query = """
                    SELECT
                        brt.ingredient_id,
                        brt.base_quantity,
                        brt.unit,
                        brt.is_required,
                        brt.notes,
                        i.name as ingredient_name
                    FROM base_recipe_templates brt
                    JOIN ingredients i ON brt.ingredient_id = i.id
                    WHERE brt.product_base_type_id = $1
                """
                ingredients_rows = await conn.fetch(ingredients_query, row['id'])

                recipes.append({
                    "id": str(row['id']),
                    "name": row['name'],
                    "description": row['description'],
                    "isActive": row['is_active'],
                    "createdAt": row['created_at'].isoformat() if row['created_at'] else None,
                    "updatedAt": row['updated_at'].isoformat() if row['updated_at'] else None,
                    "ingredients": [
                        {
                            "id": str(ing['ingredient_id']),
                            "name": ing['ingredient_name'],
                            "quantity": float(ing['base_quantity']),
                            "unit": ing['unit'],
                            "isRequired": ing['is_required'],
                            "notes": ing['notes']
                        }
                        for ing in ingredients_rows
                    ]
                })

            return {
                "success": True,
                "data": recipes,
                "pagination": {
                    "total": total_count,
                    "limit": limit,
                    "offset": offset,
                    "hasMore": (offset + limit) < total_count
                },
                "meta": {
                    "tokenId": token_id,
                    "tenantId": tenant_id
                }
            }

    except (AuthenticationError, AuthorizationError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting menu recipes via API: {str(e)}")
        raise APIError(f"Error al obtener recetas: {str(e)}", status_code=500)


async def get_customers_list(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    search: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    timezone: str = "America/Bogota",
    sort_field: str = "total_spent",
    sort_direction: str = "desc"
) -> dict:
    """
    Get list of customers aggregated from POS orders.
    Date filters scope the aggregation but never exclude customers with zero orders in the period.
    Requires scope: customers:read or read
    """
    try:
        tenant_id, token_id = validate_api_key_auth(request, "customers:read")

        # Sort validation — allowlist prevents SQL injection
        _allowed_sort = {"total_spent", "order_count", "last_order_date", "avg_ticket"}
        sort_col = sort_field if sort_field in _allowed_sort else "total_spent"
        sort_dir = "ASC" if sort_direction.lower() == "asc" else "DESC"

        async with get_db_connection(use_transaction=False) as conn:
            # $1 = tenant_id (used in both CTEs)
            params: list = [UUID(tenant_id)]
            param_count = 1

            # --- all_customers CTE: base pool (no date filter) ---
            base_conditions = [
                "o.tenant_id = $1",
                "(o.pos_cart_id IS NOT NULL OR o.extra_attributes->>'source' = 'manual')",
                "o.customer_id IS NOT NULL",
            ]

            if search:
                param_count += 1
                base_conditions.append(
                    f"(p.name ILIKE ${param_count} OR p.phone_number ILIKE ${param_count})"
                )
                params.append(f"%{search}%")

            base_where = " AND ".join(base_conditions)

            # --- period_agg CTE: date-scoped aggregation ---
            period_conditions = [
                "o.tenant_id = $1",
                "(o.pos_cart_id IS NOT NULL OR o.extra_attributes->>'source' = 'manual')",
                "o.customer_id IS NOT NULL",
            ]

            parsed_date_from = parse_date(date_from)
            parsed_date_to = parse_date(date_to)

            if parsed_date_from:
                param_count += 1
                date_from_param = param_count
                param_count += 1
                tz_from_param = param_count
                period_conditions.append(
                    f"o.order_date >= (${date_from_param}::timestamp AT TIME ZONE ${tz_from_param})"
                )
                params.append(parsed_date_from)
                params.append(timezone)

            if parsed_date_to:
                param_count += 1
                date_to_param = param_count
                param_count += 1
                tz_to_param = param_count
                period_conditions.append(
                    f"o.order_date < ((${date_to_param}::timestamp + interval '1 day') AT TIME ZONE ${tz_to_param})"
                )
                params.append(parsed_date_to)
                params.append(timezone)

            period_where = " AND ".join(period_conditions)

            param_count += 1
            limit_param = param_count
            param_count += 1
            offset_param = param_count

            query = f"""
                WITH all_customers AS (
                    SELECT DISTINCT
                        o.customer_id,
                        COALESCE(p.name, 'Sin identificar') AS name,
                        p.phone_number                       AS phone
                    FROM orders o
                    LEFT JOIN profile p ON o.customer_id = p.id
                    WHERE {base_where}
                ),
                period_agg AS (
                    SELECT
                        o.customer_id,
                        SUM(o.total_amount)  AS total_spent,
                        COUNT(o.id)          AS order_count,
                        AVG(o.total_amount)  AS avg_ticket,
                        MAX(o.order_date)    AS last_order_date
                    FROM orders o
                    WHERE {period_where}
                    GROUP BY o.customer_id
                )
                SELECT
                    ac.customer_id,
                    ac.name,
                    ac.phone,
                    COALESCE(pa.total_spent, 0)     AS total_spent,
                    COALESCE(pa.order_count, 0)     AS order_count,
                    COALESCE(pa.avg_ticket, 0)      AS avg_ticket,
                    pa.last_order_date,
                    COALESCE(ww.current_balance, 0) AS waros_balance,
                    COUNT(*) OVER()                 AS total_count
                FROM all_customers ac
                LEFT JOIN period_agg pa ON ac.customer_id = pa.customer_id
                LEFT JOIN waros_wallets ww
                    ON ww.profile_id = ac.customer_id AND ww.tenant_id = $1
                ORDER BY {sort_col} {sort_dir} NULLS LAST
                LIMIT ${limit_param} OFFSET ${offset_param}
            """

            params.extend([limit, offset])
            rows = await conn.fetch(query, *params)
            total_count = rows[0]['total_count'] if rows else 0

            customers = [
                {
                    "customer_id": str(row['customer_id']),
                    "name": row['name'],
                    "phone": row['phone'],
                    "order_count": int(row['order_count']),
                    "total_spent": float(row['total_spent']),
                    "avg_ticket": float(row['avg_ticket']),
                    "last_order_date": row['last_order_date'].isoformat() if row['last_order_date'] else None,
                    "waros_balance": int(row['waros_balance']),
                }
                for row in rows
            ]

            return {
                "data": customers,
                "total": total_count,
                "limit": limit,
                "offset": offset,
            }

    except (AuthenticationError, AuthorizationError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting customers list via API: {str(e)}")
        raise APIError(f"Error al obtener clientes: {str(e)}", status_code=500)


async def get_customer_detail(
    request: Request,
    customer_id: UUID,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    timezone: str = "America/Bogota",
    limit: int = 20,
    offset: int = 0
) -> dict:
    """
    Get a single customer's aggregate stats, paginated order history, and WaRos summary.
    Requires scope: customers:read or read
    """
    try:
        tenant_id, token_id = validate_api_key_auth(request, "customers:read")

        async with get_db_connection(use_transaction=False) as conn:
            # --- Customer aggregate stats (all-time, no date filter) ---
            customer_row = await conn.fetchrow(
                """
                SELECT
                    o.customer_id,
                    COALESCE(p.name, 'Sin identificar')                        AS name,
                    p.phone_number                                               AS phone,
                    CASE WHEN p.email LIKE '%@customer.temp' THEN NULL
                         ELSE p.email END                                        AS email,
                    COUNT(o.id)                                                  AS total_orders,
                    SUM(o.total_amount)                                          AS total_spent,
                    MIN(o.order_date)                                            AS first_purchase,
                    MAX(o.order_date)                                            AS last_purchase
                FROM orders o
                LEFT JOIN profile p ON o.customer_id = p.id
                WHERE o.tenant_id = $1
                  AND (o.pos_cart_id IS NOT NULL OR o.extra_attributes->>'source' = 'manual')
                  AND o.customer_id = $2
                GROUP BY o.customer_id, p.name, p.phone_number, p.email
                """,
                UUID(tenant_id),
                customer_id,
            )

            if not customer_row:
                raise APIError("Customer not found", status_code=404)

            # --- Paginated order history (date-filtered) ---
            where_conditions = [
                "o.tenant_id = $1",
                "(o.pos_cart_id IS NOT NULL OR o.extra_attributes->>'source' = 'manual')",
                "o.customer_id = $2",
            ]
            params: list = [UUID(tenant_id), customer_id]
            param_count = 2

            parsed_date_from = parse_date(date_from)
            parsed_date_to = parse_date(date_to)

            if parsed_date_from:
                param_count += 1
                where_conditions.append(
                    f"o.order_date >= (${param_count}::timestamp AT TIME ZONE '{timezone}')"
                )
                params.append(parsed_date_from)

            if parsed_date_to:
                param_count += 1
                where_conditions.append(
                    f"o.order_date < ((${param_count}::timestamp + interval '1 day') AT TIME ZONE '{timezone}')"
                )
                params.append(parsed_date_to)

            where_clause = " AND ".join(where_conditions)
            param_count += 1
            limit_param = param_count
            param_count += 1
            offset_param = param_count

            orders_query = f"""
                SELECT
                    o.id                AS order_id,
                    o.order_number,
                    o.order_date,
                    o.total_amount,
                    o.status,
                    o.payment_method,
                    (
                        SELECT COUNT(*)
                        FROM order_items oi
                        WHERE oi.order_id = o.id
                    )                   AS items_count,
                    (
                        SELECT COALESCE(SUM(wt.waros_amount), 0)
                        FROM waros_transactions wt
                        WHERE wt.related_entity_id = o.id::text
                          AND wt.transaction_type = 'earned'
                          AND wt.tenant_id = $1
                    )                   AS waros_earned,
                    COUNT(*) OVER()     AS total_count
                FROM orders o
                WHERE {where_clause}
                ORDER BY o.order_date DESC
                LIMIT ${limit_param} OFFSET ${offset_param}
            """

            params.append(limit)
            params.append(offset)
            order_rows = await conn.fetch(orders_query, *params)
            total_count = int(order_rows[0]['total_count']) if order_rows else 0

            orders = [
                {
                    "order_id": str(row['order_id']),
                    "order_number": int(row['order_number']),
                    "date": row['order_date'].isoformat(),
                    "total": float(row['total_amount']),
                    "items_count": int(row['items_count']),
                    "payment_method": row['payment_method'],
                    "status": row['status'],
                    "waros_earned": int(row['waros_earned']),
                }
                for row in order_rows
            ]

            # --- WaRos wallet summary ---
            wallet_row = await conn.fetchrow(
                """
                SELECT current_balance, lifetime_earned, lifetime_spent
                FROM waros_wallets
                WHERE profile_id = $1 AND tenant_id = $2
                """,
                customer_id,
                UUID(tenant_id),
            )

            return {
                "customer": {
                    "customer_id": str(customer_row['customer_id']),
                    "name": customer_row['name'],
                    "phone": customer_row['phone'],
                    "email": customer_row['email'],
                    "total_orders": int(customer_row['total_orders']),
                    "total_spent": float(customer_row['total_spent']),
                    "first_purchase": customer_row['first_purchase'].date().isoformat() if customer_row['first_purchase'] else None,
                    "last_purchase": customer_row['last_purchase'].date().isoformat() if customer_row['last_purchase'] else None,
                },
                "orders": {
                    "items": orders,
                    "total": total_count,
                    "limit": limit,
                    "offset": offset,
                },
                "waros_summary": {
                    "current_balance": int(wallet_row['current_balance']) if wallet_row else 0,
                    "lifetime_earned": int(wallet_row['lifetime_earned']) if wallet_row else 0,
                    "lifetime_spent": int(wallet_row['lifetime_spent']) if wallet_row else 0,
                },
            }

    except (AuthenticationError, AuthorizationError) as e:
        raise e
    except APIError as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting customer detail via API: {str(e)}")
        raise APIError(f"Error al obtener detalle del cliente: {str(e)}", status_code=500)


async def get_menu_modifiers(
    request: Request,
    limit: int = 50,
    offset: int = 0
) -> dict:
    """
    Get list of modifier groups with their modifiers and ingredients.
    Requires scope: menu:read or read
    """
    try:
        tenant_id, token_id = validate_api_key_auth(request, "menu:read")

        async with get_db_connection(use_transaction=False) as conn:
            # Build WHERE clause
            where_conditions = ["mg.tenant_id = $1"]
            params = [UUID(tenant_id)]
            param_count = 1

            where_clause = " AND ".join(where_conditions)

            # Get total count
            count_query = f"SELECT COUNT(*) as total FROM modifier_groups mg WHERE {where_clause}"
            count_row = await conn.fetchrow(count_query, *params)
            total_count = count_row['total']

            # Get modifier groups
            param_count += 1
            limit_param = param_count
            param_count += 1
            offset_param = param_count

            groups_query = f"""
                SELECT
                    mg.id,
                    mg.name,
                    mg.min_qty,
                    mg.max_qty,
                    mg.is_required,
                    mg.created_at,
                    mg.updated_at
                FROM modifier_groups mg
                WHERE {where_clause}
                ORDER BY mg.name ASC
                LIMIT ${limit_param} OFFSET ${offset_param}
            """
            params.extend([limit, offset])
            groups_rows = await conn.fetch(groups_query, *params)

            modifier_groups = []
            for row in groups_rows:
                # Get modifiers for this group
                modifiers_query = """
                    SELECT
                        m.id,
                        m.name,
                        m.price,
                        m.is_available,
                        m.is_default,
                        m.sort_order,
                        m.ingredient_id,
                        m.ingredient_quantity,
                        m.ingredient_unit,
                        i.name as ingredient_name
                    FROM modifiers m
                    LEFT JOIN ingredients i ON m.ingredient_id = i.id
                    WHERE m.modifier_group_id = $1
                    ORDER BY m.sort_order
                """
                modifiers_rows = await conn.fetch(modifiers_query, row['id'])

                # Get associated products
                products_query = """
                    SELECT
                        p.id,
                        p.name
                    FROM product_modifier_groups pmg
                    JOIN product p ON pmg.product_id = p.id
                    WHERE pmg.modifier_group_id = $1
                """
                products_rows = await conn.fetch(products_query, row['id'])

                modifier_groups.append({
                    "id": str(row['id']),
                    "name": row['name'],
                    "minQty": row['min_qty'],
                    "maxQty": row['max_qty'],
                    "isRequired": row['is_required'],
                    "createdAt": row['created_at'].isoformat() if row['created_at'] else None,
                    "updatedAt": row['updated_at'].isoformat() if row['updated_at'] else None,
                    "modifiers": [
                        {
                            "id": str(mod['id']),
                            "name": mod['name'],
                            "price": float(mod['price']) if mod['price'] else 0,
                            "isAvailable": mod['is_available'],
                            "isDefault": mod['is_default'],
                            "sortOrder": mod['sort_order'],
                            "ingredient": {
                                "id": str(mod['ingredient_id']),
                                "name": mod['ingredient_name'],
                                "quantity": float(mod['ingredient_quantity']) if mod['ingredient_quantity'] else None,
                                "unit": mod['ingredient_unit']
                            } if mod['ingredient_id'] else None
                        }
                        for mod in modifiers_rows
                    ],
                    "associatedProducts": [
                        {
                            "id": str(prod['id']),
                            "name": prod['name']
                        }
                        for prod in products_rows
                    ]
                })

            return {
                "success": True,
                "data": modifier_groups,
                "pagination": {
                    "total": total_count,
                    "limit": limit,
                    "offset": offset,
                    "hasMore": (offset + limit) < total_count
                },
                "meta": {
                    "tokenId": token_id,
                    "tenantId": tenant_id
                }
            }

    except (AuthenticationError, AuthorizationError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting menu modifiers via API: {str(e)}")
        raise APIError(f"Error al obtener modificadores: {str(e)}", status_code=500)


async def get_customers_metrics(
    request: Request,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    timezone: str = "America/Bogota",
    group_by: Optional[str] = None  # date, weekday, month
) -> dict:
    """
    Get aggregate customer analytics for the authenticated tenant.
    Requires scope: customers:read or read
    """
    try:
        tenant_id, token_id = validate_api_key_auth(request, "customers:read")

        async with get_db_connection(use_transaction=False) as conn:
            # Build base WHERE clause (period-scoped, completed POS orders only)
            pos_filter = "(pos_cart_id IS NOT NULL OR extra_attributes->>'source' = 'manual')"
            where_conditions = [f"tenant_id = $1", pos_filter, "status = 'completed'"]
            params: list = [UUID(tenant_id)]
            param_count = 1

            parsed_date_from = parse_date(date_from)
            parsed_date_to = parse_date(date_to)

            if parsed_date_from:
                param_count += 1
                df_param = param_count
                param_count += 1
                tz_df_param = param_count
                where_conditions.append(
                    f"order_date >= (${df_param}::timestamp AT TIME ZONE ${tz_df_param})"
                )
                params.append(parsed_date_from)
                params.append(timezone)

            if parsed_date_to:
                param_count += 1
                dt_param = param_count
                param_count += 1
                tz_dt_param = param_count
                where_conditions.append(
                    f"order_date < ((${dt_param}::timestamp + interval '1 day') AT TIME ZONE ${tz_dt_param})"
                )
                params.append(parsed_date_to)
                params.append(timezone)

            where_clause = " AND ".join(where_conditions)

            # --- Summary (2-CTE single query) ---
            # new_customers FILTER: only apply when date bounds are given
            if parsed_date_from and parsed_date_to:
                param_count += 1
                df2_param = param_count
                param_count += 1
                tz_df2_param = param_count
                param_count += 1
                dt2_param = param_count
                param_count += 1
                tz_dt2_param = param_count
                new_cust_filter = (
                    f"atf.first_ever >= (${df2_param}::timestamp AT TIME ZONE ${tz_df2_param})"
                    f" AND atf.first_ever < ((${dt2_param}::timestamp + interval '1 day') AT TIME ZONE ${tz_dt2_param})"
                )
                params.append(parsed_date_from)
                params.append(timezone)
                params.append(parsed_date_to)
                params.append(timezone)
            elif parsed_date_from:
                param_count += 1
                df2_param = param_count
                param_count += 1
                tz_df2_param = param_count
                new_cust_filter = (
                    f"atf.first_ever >= (${df2_param}::timestamp AT TIME ZONE ${tz_df2_param})"
                )
                params.append(parsed_date_from)
                params.append(timezone)
            elif parsed_date_to:
                param_count += 1
                dt2_param = param_count
                param_count += 1
                tz_dt2_param = param_count
                new_cust_filter = (
                    f"atf.first_ever < ((${dt2_param}::timestamp + interval '1 day') AT TIME ZONE ${tz_dt2_param})"
                )
                params.append(parsed_date_to)
                params.append(timezone)
            else:
                # No date filter: all customers in the all-time pool are "new" by definition
                new_cust_filter = "TRUE"

            summary_query = f"""
                WITH all_time_first AS (
                    SELECT customer_id, MIN(order_date) AS first_ever
                    FROM orders
                    WHERE tenant_id = $1
                      AND {pos_filter}
                      AND status = 'completed'
                    GROUP BY customer_id
                ),
                period_orders AS (
                    SELECT customer_id,
                           COUNT(*)            AS order_count,
                           SUM(total_amount)   AS revenue
                    FROM orders
                    WHERE {where_clause}
                    GROUP BY customer_id
                )
                SELECT
                    COUNT(*)                                                                     AS total_customers,
                    COUNT(*) FILTER (WHERE {new_cust_filter})                                    AS new_customers,
                    COALESCE(SUM(po.revenue), 0)                                                 AS total_revenue,
                    COALESCE(SUM(po.revenue)::float / NULLIF(COUNT(*), 0), 0)                    AS avg_ticket,
                    COALESCE(SUM(po.order_count)::float / NULLIF(COUNT(*), 0), 0)                AS avg_orders_per_customer
                FROM period_orders po
                JOIN all_time_first atf ON po.customer_id = atf.customer_id
            """

            summary_row = await conn.fetchrow(summary_query, *params)

            total_customers = int(summary_row['total_customers'])
            new_customers = int(summary_row['new_customers'])
            summary = {
                "total_customers": total_customers,
                "new_customers": new_customers,
                "returning_customers": total_customers - new_customers,
                "total_revenue": float(summary_row['total_revenue']),
                "avg_ticket": round(float(summary_row['avg_ticket']), 2),
                "avg_orders_per_customer": round(float(summary_row['avg_orders_per_customer']), 2),
            }

            # --- Top 10 customers ---
            top_params: list = [UUID(tenant_id)]
            top_pc = 1
            top_conditions = [f"o.tenant_id = $1", f"o.{pos_filter}", "o.status = 'completed'"]

            if parsed_date_from:
                top_pc += 1
                top_df = top_pc
                top_pc += 1
                top_tz_df = top_pc
                top_conditions.append(
                    f"o.order_date >= (${top_df}::timestamp AT TIME ZONE ${top_tz_df})"
                )
                top_params.append(parsed_date_from)
                top_params.append(timezone)

            if parsed_date_to:
                top_pc += 1
                top_dt = top_pc
                top_pc += 1
                top_tz_dt = top_pc
                top_conditions.append(
                    f"o.order_date < ((${top_dt}::timestamp + interval '1 day') AT TIME ZONE ${top_tz_dt})"
                )
                top_params.append(parsed_date_to)
                top_params.append(timezone)

            top_where = " AND ".join(top_conditions)

            top_query = f"""
                SELECT
                    o.customer_id,
                    COALESCE(p.name, 'Sin identificar')  AS name,
                    p.phone_number                        AS phone,
                    COUNT(o.id)                           AS order_count,
                    SUM(o.total_amount)                   AS total_spent
                FROM orders o
                LEFT JOIN profile p ON o.customer_id = p.id
                WHERE {top_where}
                GROUP BY o.customer_id, p.name, p.phone_number
                ORDER BY total_spent DESC
                LIMIT 10
            """

            top_rows = await conn.fetch(top_query, *top_params)
            top_customers = [
                {
                    "customer_id": str(row['customer_id']),
                    "name": row['name'],
                    "phone": row['phone'],
                    "order_count": int(row['order_count']),
                    "total_spent": float(row['total_spent']),
                }
                for row in top_rows
            ]

            response = {
                "summary": summary,
                "top_customers": top_customers,
            }

            # --- Series (only when groupBy specified) ---
            if group_by == "date":
                response["series"] = await _customer_metrics_by_date(
                    conn, where_clause, params, param_count, timezone
                )
            elif group_by == "weekday":
                response["series"] = await _customer_metrics_by_weekday(
                    conn, where_clause, params, param_count, timezone
                )
            elif group_by == "month":
                response["series"] = await _customer_metrics_by_month(
                    conn, where_clause, params, param_count, timezone
                )

            return response

    except (AuthenticationError, AuthorizationError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting customers metrics via API: {str(e)}")
        raise APIError(f"Error al obtener metricas de clientes: {str(e)}", status_code=500)


async def _customer_metrics_by_date(conn, where_clause, params, param_count, timezone):
    """Internal: customer metrics grouped by calendar date"""
    pos_filter = "(pos_cart_id IS NOT NULL OR extra_attributes->>'source' = 'manual')"
    param_count += 1
    tz_param = param_count
    series_params = list(params) + [timezone]

    query = f"""
        WITH all_time_first AS (
            SELECT customer_id,
                   DATE(MIN(order_date) AT TIME ZONE ${tz_param}) AS first_day
            FROM orders
            WHERE tenant_id = $1
              AND {pos_filter}
              AND status = 'completed'
            GROUP BY customer_id
        )
        SELECT
            DATE(o.order_date AT TIME ZONE ${tz_param})          AS period_date,
            COUNT(DISTINCT o.customer_id) FILTER (
                WHERE DATE(atf.first_day) = DATE(o.order_date AT TIME ZONE ${tz_param})
            )                                                      AS new_customers,
            COUNT(o.id)                                            AS orders,
            COALESCE(SUM(o.total_amount), 0)                       AS revenue
        FROM orders o
        JOIN all_time_first atf ON o.customer_id = atf.customer_id
        WHERE {where_clause}
        GROUP BY DATE(o.order_date AT TIME ZONE ${tz_param})
        ORDER BY period_date
    """

    rows = await conn.fetch(query, *series_params)
    return [
        {
            "period": row['period_date'].isoformat(),
            "new_customers": int(row['new_customers']),
            "orders": int(row['orders']),
            "revenue": float(row['revenue']),
        }
        for row in rows
    ]


async def _customer_metrics_by_weekday(conn, where_clause, params, param_count, timezone):
    """Internal: customer metrics grouped by day of week (0=Sunday … 6=Saturday)"""
    pos_filter = "(pos_cart_id IS NOT NULL OR extra_attributes->>'source' = 'manual')"
    param_count += 1
    tz_param = param_count
    series_params = list(params) + [timezone]

    days_map = {0: "Sunday", 1: "Monday", 2: "Tuesday", 3: "Wednesday",
                4: "Thursday", 5: "Friday", 6: "Saturday"}

    query = f"""
        WITH all_time_first AS (
            SELECT customer_id,
                   EXTRACT(DOW FROM MIN(order_date) AT TIME ZONE ${tz_param})::int AS first_dow
            FROM orders
            WHERE tenant_id = $1
              AND {pos_filter}
              AND status = 'completed'
            GROUP BY customer_id
        )
        SELECT
            EXTRACT(DOW FROM o.order_date AT TIME ZONE ${tz_param})::int   AS dow,
            COUNT(DISTINCT o.customer_id) FILTER (
                WHERE atf.first_dow = EXTRACT(DOW FROM o.order_date AT TIME ZONE ${tz_param})::int
            )                                                                AS new_customers,
            COUNT(o.id)                                                      AS orders,
            COALESCE(SUM(o.total_amount), 0)                                 AS revenue
        FROM orders o
        JOIN all_time_first atf ON o.customer_id = atf.customer_id
        WHERE {where_clause}
        GROUP BY EXTRACT(DOW FROM o.order_date AT TIME ZONE ${tz_param})::int
        ORDER BY dow
    """

    rows = await conn.fetch(query, *series_params)
    return [
        {
            "period": days_map.get(row['dow'], str(row['dow'])),
            "new_customers": int(row['new_customers']),
            "orders": int(row['orders']),
            "revenue": float(row['revenue']),
        }
        for row in rows
    ]


async def _customer_metrics_by_month(conn, where_clause, params, param_count, timezone):
    """Internal: customer metrics grouped by month (YYYY-MM)"""
    pos_filter = "(pos_cart_id IS NOT NULL OR extra_attributes->>'source' = 'manual')"
    param_count += 1
    tz_param = param_count
    series_params = list(params) + [timezone]

    query = f"""
        WITH all_time_first AS (
            SELECT customer_id,
                   TO_CHAR(MIN(order_date) AT TIME ZONE ${tz_param}, 'YYYY-MM') AS first_month
            FROM orders
            WHERE tenant_id = $1
              AND {pos_filter}
              AND status = 'completed'
            GROUP BY customer_id
        )
        SELECT
            TO_CHAR(o.order_date AT TIME ZONE ${tz_param}, 'YYYY-MM')   AS period,
            COUNT(DISTINCT o.customer_id) FILTER (
                WHERE atf.first_month = TO_CHAR(o.order_date AT TIME ZONE ${tz_param}, 'YYYY-MM')
            )                                                             AS new_customers,
            COUNT(o.id)                                                   AS orders,
            COALESCE(SUM(o.total_amount), 0)                              AS revenue
        FROM orders o
        JOIN all_time_first atf ON o.customer_id = atf.customer_id
        WHERE {where_clause}
        GROUP BY TO_CHAR(o.order_date AT TIME ZONE ${tz_param}, 'YYYY-MM')
        ORDER BY period
    """

    rows = await conn.fetch(query, *series_params)
    return [
        {
            "period": row['period'],
            "new_customers": int(row['new_customers']),
            "orders": int(row['orders']),
            "revenue": float(row['revenue']),
        }
        for row in rows
    ]

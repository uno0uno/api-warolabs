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
    timezone: str = "America/Bogota"
) -> dict:
    """
    Get sales metrics for the authenticated tenant via API key.
    Requires scope: orders:read or read
    """
    try:
        tenant_id, token_id = validate_api_key_auth(request, "orders:read")

        async with get_db_connection(use_transaction=False) as conn:
            # Build WHERE clause
            where_conditions = ["tenant_id = $1", "pos_cart_id IS NOT NULL"]
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

            metrics_query = f"""
                SELECT
                    COUNT(*) as total_orders,
                    COUNT(*) FILTER (WHERE status = 'completed') as completed_orders,
                    COUNT(*) FILTER (WHERE status = 'cancelled') as cancelled_orders,
                    COUNT(*) FILTER (WHERE status = 'pending') as pending_orders,
                    COALESCE(SUM(total_amount) FILTER (WHERE status = 'completed'), 0) as total_sales,
                    COALESCE(AVG(total_amount) FILTER (WHERE status = 'completed'), 0) as avg_ticket
                FROM orders
                WHERE {where_clause}
            """

            row = await conn.fetchrow(metrics_query, *params)

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
                "meta": {
                    "tokenId": token_id,
                    "tenantId": tenant_id,
                    "dateFrom": date_from,
                    "dateTo": date_to,
                    "timezone": timezone
                }
            }

    except (AuthenticationError, AuthorizationError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting sales metrics via API: {str(e)}")
        raise APIError(f"Error al obtener metricas: {str(e)}", status_code=500)


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

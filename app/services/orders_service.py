"""
Orders Service
Handles listing and filtering of POS orders
"""
from typing import List, Optional
from uuid import UUID
from fastapi import Request
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError, APIError
from app.services.aws_ses_service import ses_service
from datetime import datetime
import csv
import io
import logging

logger = logging.getLogger(__name__)


async def get_orders_list(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    search: Optional[str] = None,
    search_field: Optional[str] = None,
    payment_method: Optional[str] = None,
    status: Optional[str] = None,
    sort_field: str = "order_date",
    sort_direction: str = "desc",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None
) -> dict:
    """
    Get list of POS orders with filters and pagination
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Build WHERE clause
            where_conditions = ["o.tenant_id = $1", "o.pos_cart_id IS NOT NULL"]
            params = [tenant_id]
            param_count = 1

            # Search filter
            if search and search_field:
                param_count += 1
                if search_field == "order_number":
                    where_conditions.append(f"CAST(o.order_number AS TEXT) ILIKE ${param_count}")
                    params.append(f"%{search}%")
                elif search_field == "customer_name":
                    where_conditions.append(f"p.name ILIKE ${param_count}")
                    params.append(f"%{search}%")
                elif search_field == "customer_phone":
                    where_conditions.append(f"p.phone_number ILIKE ${param_count}")
                    params.append(f"%{search}%")

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
            if date_from:
                param_count += 1
                where_conditions.append(f"o.order_date >= ${param_count}::date")
                params.append(date_from)

            if date_to:
                param_count += 1
                where_conditions.append(f"o.order_date <= (${param_count}::date + interval '1 day')")
                params.append(date_to)

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

            # Get orders
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
                    o.pos_cart_id,
                    p.id as customer_id,
                    p.name as customer_name,
                    p.phone_number as customer_phone,
                    (
                        SELECT COUNT(*)
                        FROM order_items oi
                        WHERE oi.order_id = o.id
                    ) as items_count
                FROM orders o
                LEFT JOIN profile p ON o.customer_id = p.id
                WHERE {where_clause}
                ORDER BY {sort_column} {sort_direction}
                LIMIT ${limit_param} OFFSET ${offset_param}
            """

            params.extend([limit, offset])
            orders_rows = await conn.fetch(orders_query, *params)

            orders = [
                {
                    "id": str(row['id']),
                    "order_number": int(row['order_number']),
                    "order_date": row['order_date'].isoformat(),
                    "total_amount": float(row['total_amount']),
                    "status": row['status'],
                    "payment_method": row['payment_method'],
                    "pos_cart_id": str(row['pos_cart_id']) if row['pos_cart_id'] else None,
                    "customer": {
                        "id": str(row['customer_id']) if row['customer_id'] else None,
                        "name": row['customer_name'],
                        "phone": row['customer_phone']
                    },
                    "items_count": row['items_count']
                }
                for row in orders_rows
            ]

            return {
                "success": True,
                "data": orders,
                "pagination": {
                    "total": total_count,
                    "limit": limit,
                    "offset": offset,
                    "has_more": (offset + limit) < total_count
                }
            }

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting orders list: {str(e)}")
        raise APIError(f"Error getting orders list: {str(e)}", status_code=500)


async def get_order_by_id(
    request: Request,
    order_id: UUID
) -> dict:
    """
    Get single order by ID with customer details
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            order_query = """
                SELECT
                    o.id,
                    o.order_number,
                    o.order_date,
                    o.total_amount,
                    o.status,
                    o.payment_method,
                    o.pos_cart_id,
                    p.id as customer_id,
                    p.name as customer_name,
                    p.phone_number as customer_phone,
                    (
                        SELECT COUNT(*)
                        FROM order_items oi
                        WHERE oi.order_id = o.id
                    ) as items_count
                FROM orders o
                LEFT JOIN profile p ON o.customer_id = p.id
                WHERE o.id = $1 AND o.tenant_id = $2 AND o.pos_cart_id IS NOT NULL
            """

            order_row = await conn.fetchrow(order_query, order_id, tenant_id)

            if not order_row:
                raise APIError("Order not found", status_code=404)

            return {
                "success": True,
                "data": {
                    "id": str(order_row['id']),
                    "order_number": int(order_row['order_number']),
                    "order_date": order_row['order_date'].isoformat(),
                    "total_amount": float(order_row['total_amount']),
                    "status": order_row['status'],
                    "payment_method": order_row['payment_method'],
                    "pos_cart_id": str(order_row['pos_cart_id']) if order_row['pos_cart_id'] else None,
                    "customer": {
                        "id": str(order_row['customer_id']) if order_row['customer_id'] else None,
                        "name": order_row['customer_name'],
                        "phone": order_row['customer_phone']
                    },
                    "items_count": order_row['items_count']
                }
            }

    except (AuthenticationError, APIError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting order by ID: {str(e)}")
        raise APIError(f"Error getting order by ID: {str(e)}", status_code=500)


async def get_order_items(
    request: Request,
    order_id: UUID
) -> dict:
    """
    Get order items with modifiers for a specific order
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # First verify order exists and belongs to this tenant
            verify_query = """
                SELECT id FROM orders
                WHERE id = $1 AND tenant_id = $2 AND pos_cart_id IS NOT NULL
            """
            order_exists = await conn.fetchrow(verify_query, order_id, tenant_id)

            if not order_exists:
                raise APIError("Order not found", status_code=404)

            # Get order items with product details
            items_query = """
                SELECT
                    oi.id,
                    oi.quantity,
                    oi.price_at_purchase,
                    oi.subtotal,
                    p.id as product_id,
                    p.name as product_name,
                    p.description as product_description
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
                        id,
                        modifier_id,
                        modifier_name,
                        price_at_purchase
                    FROM order_item_modifiers
                    WHERE order_item_id = $1
                """
                modifiers_rows = await conn.fetch(modifiers_query, item_row['id'])

                modifiers = [
                    {
                        "id": str(mod['modifier_id']) if mod['modifier_id'] else None,
                        "name": mod['modifier_name'],
                        "price": float(mod['price_at_purchase'])
                    }
                    for mod in modifiers_rows
                ]

                items.append({
                    "id": str(item_row['id']),
                    "quantity": item_row['quantity'],
                    "price_at_purchase": float(item_row['price_at_purchase']),
                    "subtotal": float(item_row['subtotal']),
                    "product": {
                        "id": str(item_row['product_id']) if item_row['product_id'] else None,
                        "name": item_row['product_name'],
                        "description": item_row['product_description'],
                        "image_url": None
                    },
                    "modifiers": modifiers
                })

            return {
                "success": True,
                "data": items
            }

    except (AuthenticationError, APIError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting order items: {str(e)}")
        raise APIError(f"Error getting order items: {str(e)}", status_code=500)


async def get_orders_metrics(
    request: Request,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None
) -> dict:
    """
    Get sales metrics: total sales, average ticket, orders count by status
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Build WHERE clause
            where_conditions = ["tenant_id = $1", "pos_cart_id IS NOT NULL"]
            params = [tenant_id]
            param_count = 1

            if date_from:
                param_count += 1
                where_conditions.append(f"order_date >= ${param_count}::date")
                params.append(date_from)

            if date_to:
                param_count += 1
                where_conditions.append(f"order_date <= (${param_count}::date + interval '1 day')")
                params.append(date_to)

            where_clause = " AND ".join(where_conditions)

            metrics_query = f"""
                SELECT
                    COUNT(*) as total_orders,
                    COUNT(*) FILTER (WHERE status = 'completed') as completed_orders,
                    COUNT(*) FILTER (WHERE status = 'cancelled') as cancelled_orders,
                    COUNT(*) FILTER (WHERE status = 'pending') as pending_orders,
                    COALESCE(SUM(total_amount) FILTER (WHERE status = 'completed'), 0) as total_sales,
                    COALESCE(AVG(total_amount) FILTER (WHERE status = 'completed'), 0) as avg_ticket,
                    COALESCE(SUM(
                        (SELECT COUNT(*) FROM order_items oi WHERE oi.order_id = o.id)
                    ) FILTER (WHERE status = 'completed'), 0) as total_items
                FROM orders o
                WHERE {where_clause}
            """

            row = await conn.fetchrow(metrics_query, *params)

            return {
                "success": True,
                "data": {
                    "total_sales": float(row['total_sales']),
                    "total_orders": row['total_orders'],
                    "completed_orders": row['completed_orders'],
                    "cancelled_orders": row['cancelled_orders'],
                    "pending_orders": row['pending_orders'],
                    "avg_ticket": float(row['avg_ticket']),
                    "total_items": row['total_items']
                }
            }

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting orders metrics: {str(e)}")
        raise APIError(f"Error getting orders metrics: {str(e)}", status_code=500)


async def export_orders_to_email(
    request: Request,
    search: Optional[str] = None,
    search_field: Optional[str] = None,
    payment_method: Optional[str] = None,
    status: Optional[str] = None,
    sort_field: str = "order_date",
    sort_direction: str = "desc",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None
) -> dict:
    """
    Export all orders (without pagination) based on filters and send via email
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_email = session_context.email
        user_name = session_context.name or "Usuario"

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        if not user_email:
            raise APIError("No se encontró el correo del usuario", status_code=400)

        async with get_db_connection() as conn:
            # Build WHERE clause (same as get_orders_list but without pagination)
            where_conditions = ["o.tenant_id = $1", "o.pos_cart_id IS NOT NULL"]
            params = [tenant_id]
            param_count = 1

            # Search filter
            if search and search_field:
                param_count += 1
                if search_field == "order_number":
                    where_conditions.append(f"CAST(o.order_number AS TEXT) ILIKE ${param_count}")
                    params.append(f"%{search}%")
                elif search_field == "customer_name":
                    where_conditions.append(f"p.name ILIKE ${param_count}")
                    params.append(f"%{search}%")
                elif search_field == "customer_phone":
                    where_conditions.append(f"p.phone_number ILIKE ${param_count}")
                    params.append(f"%{search}%")

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
            if date_from:
                param_count += 1
                where_conditions.append(f"o.order_date >= ${param_count}::date")
                params.append(date_from)

            if date_to:
                param_count += 1
                where_conditions.append(f"o.order_date <= (${param_count}::date + interval '1 day')")
                params.append(date_to)

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

            # Get ALL orders without pagination
            orders_query = f"""
                SELECT
                    o.id,
                    o.order_number,
                    o.order_date,
                    o.total_amount,
                    o.status,
                    o.payment_method,
                    p.name as customer_name,
                    p.phone_number as customer_phone,
                    (
                        SELECT COUNT(*)
                        FROM order_items oi
                        WHERE oi.order_id = o.id
                    ) as items_count
                FROM orders o
                LEFT JOIN profile p ON o.customer_id = p.id
                WHERE {where_clause}
                ORDER BY {sort_column} {sort_direction}
            """

            orders_rows = await conn.fetch(orders_query, *params)

            if not orders_rows:
                raise APIError("No hay ventas para exportar con los filtros seleccionados", status_code=404)

            # Payment method labels
            payment_labels = {
                'cash': 'Efectivo',
                'card': 'Tarjeta',
                'digital': 'Digital'
            }

            # Status labels
            status_labels = {
                'completed': 'Completada',
                'cancelled': 'Cancelada',
                'pending': 'Pendiente'
            }

            # Generate CSV with Excel-friendly formatting
            now = datetime.now()
            output = io.StringIO()
            writer = csv.writer(output, delimiter=';')  # Use semicolon for better Excel compatibility

            # Write header
            writer.writerow([
                'Numero Orden',
                'Fecha',
                'Hora',
                'Cliente',
                'Telefono',
                'Items',
                'Metodo Pago',
                'Total',
                'Estado'
            ])

            total_sum = 0
            completed_count = 0
            cancelled_count = 0

            for row in orders_rows:
                # Split date and time for easier Excel filtering
                order_date = row['order_date'].strftime('%Y-%m-%d') if row['order_date'] else ''
                order_time = row['order_date'].strftime('%H:%M:%S') if row['order_date'] else ''
                total_amount = float(row['total_amount'])

                if row['status'] == 'completed':
                    total_sum += total_amount
                    completed_count += 1
                elif row['status'] == 'cancelled':
                    cancelled_count += 1

                writer.writerow([
                    row['order_number'],
                    order_date,
                    order_time,
                    row['customer_name'] or 'Sin nombre',
                    row['customer_phone'] or '',
                    row['items_count'],
                    payment_labels.get(row['payment_method'], row['payment_method']),
                    total_amount,
                    status_labels.get(row['status'], row['status'])
                ])

            csv_content = output.getvalue()
            output.close()

            # Build filter description for email
            filter_desc = []
            if date_from and date_to:
                filter_desc.append(f"Período: {date_from} al {date_to}")
            elif date_from:
                filter_desc.append(f"Desde: {date_from}")
            elif date_to:
                filter_desc.append(f"Hasta: {date_to}")
            if status:
                filter_desc.append(f"Estado: {status_labels.get(status, status)}")
            if payment_method:
                filter_desc.append(f"Método de pago: {payment_labels.get(payment_method, payment_method)}")
            if search:
                filter_desc.append(f"Búsqueda: {search}")

            filter_text = "\n".join(filter_desc) if filter_desc else "Sin filtros aplicados"

            date_str = now.strftime('%Y-%m-%d_%H%M')

            email_body = f"""¡Hola {user_name}!

Aquí está tu reporte de ventas solicitado.

RESUMEN
-------
Total de ventas exportadas: {len(orders_rows)}
Ventas completadas: {completed_count}
Ventas canceladas: {cancelled_count}
Monto total (completadas): ${total_sum:,.0f}
Fecha de generación: {now.strftime('%d/%m/%Y %H:%M')}

FILTROS APLICADOS
-----------------
{filter_text}

Adjunto encontrarás el archivo CSV con el detalle de las ventas.
Puedes abrirlo con Excel o Google Sheets.

---
Saifer 101 de Waro Colombia
Tecnología colombiana para el mundo.
"""

            # Send email with CSV attachment
            filename = f"ventas_{date_str}.csv"
            success = await ses_service.send_email_with_attachment(
                from_email="hola@warocol.com",
                from_name="Waro Colombia - Reportes",
                to_emails=[user_email],
                subject=f"Reporte de Ventas - {date_str}",
                text_body=email_body,
                attachment_data=csv_content.encode('utf-8'),
                attachment_filename=filename,
                attachment_type="text/csv"
            )

            if not success:
                raise APIError("Error al enviar el correo. Intenta de nuevo.", status_code=500)

            return {
                "success": True,
                "message": f"Reporte enviado a {user_email}",
                "data": {
                    "email": user_email,
                    "orders_count": len(orders_rows),
                    "total_sales": total_sum
                }
            }

    except (AuthenticationError, APIError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error exporting orders: {str(e)}")
        raise APIError(f"Error al exportar ventas: {str(e)}", status_code=500)

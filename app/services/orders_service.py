"""
Orders Service
Handles listing and filtering of POS orders
"""
import asyncio
from typing import Any, Dict, Optional, List
from uuid import UUID
from fastapi import Request
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError, APIError
from app.services.aws_ses_service import ses_service
from app.services.waros_service import evaluate_and_award
from app.services.cierre_service import assert_order_not_in_closed_monthly_period, _get_tenant_tax_config
from app.services.email_helpers import send_pos_receipt_email
from fastapi import HTTPException
from datetime import datetime, date
import csv


import io
import logging


def parse_date(date_str: Optional[str]) -> Optional[date]:
    """Convert date string (YYYY-MM-DD) to date object"""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return None

logger = logging.getLogger(__name__)


POS_LIKE_FILTER = (
    "(pos_cart_id IS NOT NULL OR table_session_id IS NOT NULL "
    "OR extra_attributes->>'source' = 'manual')"
)
POS_LIKE_FILTER_ALIAS_O = (
    "(o.pos_cart_id IS NOT NULL OR o.table_session_id IS NOT NULL "
    "OR o.extra_attributes->>'source' = 'manual')"
)


def _compute_tax_breakdown(
    items_rows: List[Any],
    tax_config: Dict[str, Any],
) -> tuple:
    """
    Compute standard_tax, liquor_tax, and standard_tax_label from a list of rows
    with .tax_category and .subtotal fields.

    Works with both item-level rows (one row per order item) and pre-aggregated
    rows (one row per tax_category, subtotal already summed by SQL).

    Returns: (standard_tax: float, liquor_tax: float, standard_tax_label: str)
    """
    std_subtotal = sum(float(r['subtotal']) for r in items_rows if r['tax_category'] == 'standard')
    liq_subtotal = sum(float(r['subtotal']) for r in items_rows if r['tax_category'] == 'liquor')

    standard_tax = 0.0
    liquor_tax = 0.0
    standard_tax_label = "Impuesto"

    if tax_config.get('inc_applicable') and std_subtotal > 0:
        rate = float(tax_config['inc_rate'])
        if tax_config.get('inc_included_in_price'):
            standard_tax = round(std_subtotal * rate / (1 + rate))
        else:
            standard_tax = round(std_subtotal * rate)
        standard_tax_label = f"INC {round(rate * 100)}%"
    elif tax_config.get('iva_applicable') and std_subtotal > 0:
        rate = float(tax_config['iva_rate'])
        if tax_config.get('iva_included_in_price'):
            standard_tax = round(std_subtotal * rate / (1 + rate))
        else:
            standard_tax = round(std_subtotal * rate)
        standard_tax_label = f"IVA {round(rate * 100)}%"

    if tax_config.get('liquor_tax_applicable') and liq_subtotal > 0:
        liq_rate = float(tax_config.get('liquor_tax_rate') or 0.05)
        liquor_tax = round(liq_subtotal * liq_rate)

    return float(standard_tax), float(liquor_tax), standard_tax_label


async def get_orders_list(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    search: Optional[str] = None,
    search_field: Optional[str] = None,
    payment_method: Optional[str] = None,
    payment_method_id: Optional[str] = None,
    status: Optional[str] = None,
    sort_field: str = "order_date",
    sort_direction: str = "desc",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    delivery_only: Optional[bool] = None,
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
            where_conditions = [
                "o.tenant_id = $1",
                "(o.pos_cart_id IS NOT NULL OR o.table_session_id IS NOT NULL OR o.extra_attributes->>'source' = 'manual')"
            ]
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

            # Payment method ID filter (specific custom method)
            if payment_method_id:
                param_count += 1
                where_conditions.append(f"o.payment_method_id = ${param_count}::uuid")
                params.append(payment_method_id)

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
                where_conditions.append(f"o.order_date >= (${param_count}::timestamp AT TIME ZONE 'America/Bogota')")
                params.append(parsed_date_from)

            if parsed_date_to:
                param_count += 1
                where_conditions.append(f"o.order_date < ((${param_count}::timestamp + interval '1 day') AT TIME ZONE 'America/Bogota')")
                params.append(parsed_date_to)

            # Delivery-only filter: composes with POS_LIKE_FILTER, uses partial index idx_orders_delivery_address_id
            if delivery_only:
                where_conditions.append("o.delivery_address_id IS NOT NULL")

            where_clause = " AND ".join(where_conditions)

            # Validate sort field
            allowed_sort_fields = ["order_number", "order_date", "total_amount", "customer_name", "payment_method", "discount_amount"]
            if sort_field not in allowed_sort_fields:
                sort_field = "order_date"

            sort_direction = "ASC" if sort_direction.lower() == "asc" else "DESC"

            # Map sort field to actual column
            sort_column_map = {
                "order_number": "o.order_number",
                "order_date": "o.order_date",
                "total_amount": "o.total_amount",
                "customer_name": "p.name",
                "payment_method": "o.payment_method",
                "discount_amount": "o.discount_amount"
            }
            sort_column = sort_column_map.get(sort_field, "o.order_date")

            # Single query: COUNT(*) OVER() replaces separate count round-trip.
            # items_count uses correlated subquery (idx_order_items_order_id exists).
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
                    o.payment_method_id,
                    o.payment_status,
                    o.credit_due_date,
                    o.credit_paid_amount,
                    o.discount_amount,
                    o.pos_cart_id,
                    o.table_session_id,
                    o.delivery_address_id,
                    o.scheduled_time,
                    o.delivery_instructions,
                    t_meta.is_bar as is_bar,
                    p.id as customer_id,
                    p.name as customer_name,
                    p.phone_number as customer_phone,
                    (
                        SELECT COUNT(*)
                        FROM order_items oi
                        WHERE oi.order_id = o.id
                    ) as items_count,
                    (
                        SELECT COUNT(*)
                        FROM order_payments op
                        WHERE op.order_id = o.id
                    ) as split_payments_count,
                    COUNT(*) OVER() as total_count
                FROM orders o
                LEFT JOIN profile p ON o.customer_id = p.id
                LEFT JOIN table_sessions ts_meta ON ts_meta.id = o.table_session_id
                LEFT JOIN tables t_meta ON t_meta.id = ts_meta.table_id
                WHERE {where_clause}
                ORDER BY {sort_column} {sort_direction}
                LIMIT ${limit_param} OFFSET ${offset_param}
            """

            params.extend([limit, offset])
            orders_rows = await conn.fetch(orders_query, *params)
            total_count = orders_rows[0]['total_count'] if orders_rows else 0

            orders = [
                {
                    "id": str(row['id']),
                    "order_number": int(row['order_number']),
                    "order_date": row['order_date'].isoformat(),
                    "total_amount": float(row['total_amount']),
                    "status": row['status'],
                    "payment_method": row['payment_method'],
                    "payment_method_id": str(row['payment_method_id']) if row['payment_method_id'] else None,
                    "payment_status": row['payment_status'],
                    "credit_due_date": str(row['credit_due_date']) if row['credit_due_date'] is not None else None,
                    "credit_paid_amount": float(row['credit_paid_amount']) if row['credit_paid_amount'] is not None else 0.0,
                    "discount_amount": float(row['discount_amount']) if row['discount_amount'] is not None else 0.0,
                    "pos_cart_id": str(row['pos_cart_id']) if row['pos_cart_id'] else None,
                    "source": (
                        "barra" if row['table_session_id'] and row['is_bar'] else
                        "mesa" if row['table_session_id'] else
                        "pos"
                    ),
                    "delivery_address_id": str(row['delivery_address_id']) if row['delivery_address_id'] else None,
                    "scheduled_time": row['scheduled_time'].isoformat() if row['scheduled_time'] else None,
                    "delivery_instructions": row['delivery_instructions'],
                    "is_delivery": row['delivery_address_id'] is not None,
                    "customer": {
                        "id": str(row['customer_id']) if row['customer_id'] else None,
                        "name": row['customer_name'],
                        "phone": row['customer_phone']
                    },
                    "items_count": row['items_count'],
                    "split_payments_count": int(row['split_payments_count'])
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


async def get_tips_list(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    search: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    member_id: Optional[UUID] = None,
    payment_method: Optional[str] = None,
    channel: Optional[str] = None,
    sort_field: str = "order_date",
    sort_direction: str = "desc",
) -> dict:
    """
    Get list of orders that captured a tip, with filters, pagination, and
    aggregates. Powers /ventas/propinas (warocol.com#640).

    Base WHERE: tenant scope + tip_amount > 0. The aggregates query runs against
    the same WHERE so the totals always match the visible page.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            where_conditions = [
                "o.tenant_id = $1",
                "o.tip_amount > 0",
            ]
            params: List[Any] = [tenant_id]
            param_count = 1

            # Free-text search on order_number
            if search:
                param_count += 1
                where_conditions.append(f"CAST(o.order_number AS TEXT) ILIKE ${param_count}")
                params.append(f"%{search}%")

            if payment_method:
                param_count += 1
                where_conditions.append(f"o.payment_method = ${param_count}")
                params.append(payment_method)

            if member_id:
                param_count += 1
                where_conditions.append(f"o.served_by_member_id = ${param_count}::uuid")
                params.append(str(member_id))

            # channel ∈ {'online', 'mesa', 'pos'} mirrors the derivation in get_orders_list
            if channel == 'online':
                where_conditions.append("o.online_cart_id IS NOT NULL")
            elif channel == 'mesa':
                where_conditions.append("o.table_session_id IS NOT NULL")
            elif channel == 'pos':
                where_conditions.append("o.pos_cart_id IS NOT NULL AND o.table_session_id IS NULL")

            parsed_date_from = parse_date(date_from)
            parsed_date_to = parse_date(date_to)

            if parsed_date_from:
                param_count += 1
                where_conditions.append(f"o.order_date >= (${param_count}::timestamp AT TIME ZONE 'America/Bogota')")
                params.append(parsed_date_from)

            if parsed_date_to:
                param_count += 1
                where_conditions.append(f"o.order_date < ((${param_count}::timestamp + interval '1 day') AT TIME ZONE 'America/Bogota')")
                params.append(parsed_date_to)

            where_clause = " AND ".join(where_conditions)

            allowed_sort_fields = ["order_number", "order_date", "total_amount", "tip_amount", "payment_method"]
            if sort_field not in allowed_sort_fields:
                sort_field = "order_date"
            sort_direction_sql = "ASC" if sort_direction.lower() == "asc" else "DESC"
            sort_column_map = {
                "order_number": "o.order_number",
                "order_date": "o.order_date",
                "total_amount": "o.total_amount",
                "tip_amount": "o.tip_amount",
                "payment_method": "o.payment_method",
            }
            sort_column = sort_column_map.get(sort_field, "o.order_date")

            param_count += 1
            limit_param = param_count
            param_count += 1
            offset_param = param_count

            tips_query = f"""
                SELECT
                    o.id,
                    o.order_number,
                    o.order_date,
                    o.total_amount,
                    o.tip_amount,
                    o.tip_source,
                    o.payment_method,
                    o.payment_method_id,
                    o.pos_cart_id,
                    o.online_cart_id,
                    o.table_session_id,
                    o.served_by_member_id,
                    p.name AS member_name,
                    t_meta.is_bar AS is_bar,
                    COUNT(*) OVER() AS total_count
                FROM orders o
                LEFT JOIN tenant_members tm ON tm.id = o.served_by_member_id
                LEFT JOIN profile p ON p.id = tm.user_id
                LEFT JOIN table_sessions ts_meta ON ts_meta.id = o.table_session_id
                LEFT JOIN tables t_meta ON t_meta.id = ts_meta.table_id
                WHERE {where_clause}
                ORDER BY {sort_column} {sort_direction_sql}
                LIMIT ${limit_param} OFFSET ${offset_param}
            """

            row_params = list(params) + [limit, offset]
            rows = await conn.fetch(tips_query, *row_params)
            total_count = rows[0]['total_count'] if rows else 0

            # Aggregates run against the same WHERE (no LIMIT/OFFSET, no pagination params)
            aggregates_query = f"""
                SELECT
                    COALESCE(SUM(o.tip_amount), 0) AS sum_tip,
                    COUNT(*) AS count_with_tip,
                    COALESCE(AVG(
                        CASE WHEN o.total_amount > 0
                             THEN o.tip_amount / o.total_amount * 100
                             ELSE 0
                        END
                    ), 0) AS avg_pct
                FROM orders o
                WHERE {where_clause}
            """
            agg_row = await conn.fetchrow(aggregates_query, *params)

            tips = [
                {
                    "id": str(row['id']),
                    "order_number": int(row['order_number']),
                    "order_date": row['order_date'].isoformat(),
                    "total_amount": float(row['total_amount']),
                    "tip_amount": float(row['tip_amount']),
                    "tip_source": row['tip_source'],
                    "tip_percent": (
                        round(float(row['tip_amount']) / float(row['total_amount']) * 100, 2)
                        if float(row['total_amount']) > 0 else 0.0
                    ),
                    "payment_method": row['payment_method'],
                    "payment_method_id": str(row['payment_method_id']) if row['payment_method_id'] else None,
                    "channel": (
                        "online" if row['online_cart_id'] else
                        "barra" if row['table_session_id'] and row['is_bar'] else
                        "mesa" if row['table_session_id'] else
                        "pos"
                    ),
                    "served_by_member_id": str(row['served_by_member_id']) if row['served_by_member_id'] else None,
                    "member_name": row['member_name'],
                }
                for row in rows
            ]

            return {
                "success": True,
                "data": tips,
                "pagination": {
                    "total": total_count,
                    "limit": limit,
                    "offset": offset,
                    "has_more": (offset + limit) < total_count,
                },
                "aggregates": {
                    "sum_tip": float(agg_row['sum_tip']) if agg_row else 0.0,
                    "count_with_tip": int(agg_row['count_with_tip']) if agg_row else 0,
                    "avg_pct": round(float(agg_row['avg_pct']), 2) if agg_row else 0.0,
                },
            }

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting tips list: {str(e)}")
        raise APIError(f"Error getting tips list: {str(e)}", status_code=500)


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
                    o.payment_method_id,
                    o.payment_status,
                    o.credit_due_date,
                    o.credit_paid_amount,
                    o.discount_amount,
                    o.discount_type,
                    o.discount_value,
                    o.pos_cart_id,
                    o.table_session_id,
                    o.delivery_address_id,
                    o.scheduled_time,
                    o.delivery_instructions,
                    t_meta2.is_bar as is_bar,
                    o.served_by_member_id,
                    p_served.name as served_by_member_name,
                    p.id as customer_id,
                    p.name as customer_name,
                    p.phone_number as customer_phone,
                    p.email as customer_email,
                    -- Hydrated delivery address (NULL if not a delivery, or address was soft-deleted)
                    ap.address_line1   AS addr_line1,
                    ap.address_line2   AS addr_line2,
                    ap.city            AS addr_city,
                    ap.state           AS addr_state,
                    ap.postal_code     AS addr_postal_code,
                    ap.country         AS addr_country,
                    ap.latitude        AS addr_latitude,
                    ap.longitude       AS addr_longitude,
                    ap.label           AS addr_type,
                    ap.delivery_notes  AS addr_delivery_notes,
                    (
                        SELECT COUNT(*)
                        FROM order_items oi
                        WHERE oi.order_id = o.id
                    ) as items_count
                FROM orders o
                LEFT JOIN tenant_members tm_served ON tm_served.id = o.served_by_member_id
                LEFT JOIN profile p_served ON p_served.id = tm_served.user_id
                LEFT JOIN profile p ON o.customer_id = p.id
                LEFT JOIN table_sessions ts_meta2 ON ts_meta2.id = o.table_session_id
                LEFT JOIN tables t_meta2 ON t_meta2.id = ts_meta2.table_id
                LEFT JOIN addresses_profile ap ON ap.id = o.delivery_address_id AND ap.deleted_at IS NULL
                WHERE o.id = $1 AND o.tenant_id = $2
                  AND (o.pos_cart_id IS NOT NULL OR o.table_session_id IS NOT NULL OR o.extra_attributes->>'source' = 'manual')

            """

            order_row = await conn.fetchrow(order_query, order_id, tenant_id)

            if not order_row:
                raise APIError("Order not found", status_code=404)

            # Fetch split payments if any
            payments_rows = await conn.fetch(
                """
                SELECT id, amount, payment_method, payment_method_id, paid_at
                FROM order_payments
                WHERE order_id = $1
                ORDER BY paid_at ASC
                """,
                order_id
            )
            split_payments = [
                {
                    "id": str(r['id']),
                    "amount": float(r['amount']),
                    "payment_method": r['payment_method'],
                    "payment_method_id": str(r['payment_method_id']) if r['payment_method_id'] else None,
                    "paid_at": r['paid_at'].isoformat(),
                }
                for r in payments_rows
            ]

            # Tax breakdown
            _std_tax = 0.0
            _liq_tax = 0.0
            _tax_label = "Impuesto"
            try:
                tax_config = await _get_tenant_tax_config(conn, tenant_id)
                items_rows = await conn.fetch(
                    """SELECT COALESCE(p.tax_category, 'standard') AS tax_category,
                              COALESCE(oi.subtotal, 0) AS subtotal
                       FROM order_items oi
                       JOIN product p ON p.id = oi.product_id
                       WHERE oi.order_id = $1""",
                    order_id
                )
                _std_tax, _liq_tax, _tax_label = _compute_tax_breakdown(items_rows, tax_config)
            except Exception as _e:
                logger.warning(f"Tax breakdown failed for order {order_id}: {_e}")

            # Hydrate delivery address inline (None if not a delivery, or address was soft-deleted)
            delivery_address = None
            if order_row['delivery_address_id'] and order_row['addr_line1'] is not None:
                delivery_address = {
                    "id": str(order_row['delivery_address_id']),
                    "address_line1": order_row['addr_line1'],
                    "address_line2": order_row['addr_line2'],
                    "city": order_row['addr_city'],
                    "state": order_row['addr_state'],
                    "postal_code": order_row['addr_postal_code'],
                    "country": order_row['addr_country'],
                    "latitude": float(order_row['addr_latitude']) if order_row['addr_latitude'] is not None else None,
                    "longitude": float(order_row['addr_longitude']) if order_row['addr_longitude'] is not None else None,
                    "address_type": order_row['addr_type'],
                    "delivery_notes": order_row['addr_delivery_notes'],
                }

            return {
                "success": True,
                "data": {
                    "id": str(order_row['id']),
                    "order_number": int(order_row['order_number']),
                    "order_date": order_row['order_date'].isoformat(),
                    "total_amount": float(order_row['total_amount']),
                    "status": order_row['status'],
                    "payment_method": order_row['payment_method'],
                    "payment_method_id": str(order_row['payment_method_id']) if order_row['payment_method_id'] else None,
                    "payment_status": order_row['payment_status'],
                    "credit_due_date": str(order_row['credit_due_date']) if order_row['credit_due_date'] is not None else None,
                    "credit_paid_amount": float(order_row['credit_paid_amount']) if order_row['credit_paid_amount'] is not None else 0.0,
                    "discount_amount": float(order_row['discount_amount']) if order_row['discount_amount'] is not None else 0.0,
                    "discount_type": order_row['discount_type'],
                    "discount_value": float(order_row['discount_value']) if order_row['discount_value'] is not None else None,
                    "pos_cart_id": str(order_row['pos_cart_id']) if order_row['pos_cart_id'] else None,
                    "source": (
                        "barra" if order_row['table_session_id'] and order_row['is_bar'] else
                        "mesa" if order_row['table_session_id'] else
                        "pos"
                    ),
                    "delivery_address_id": str(order_row['delivery_address_id']) if order_row['delivery_address_id'] else None,
                    "delivery_address": delivery_address,
                    "scheduled_time": order_row['scheduled_time'].isoformat() if order_row['scheduled_time'] else None,
                    "delivery_instructions": order_row['delivery_instructions'],
                    "is_delivery": order_row['delivery_address_id'] is not None,
                    "customer": {
                        "id": str(order_row['customer_id']) if order_row['customer_id'] else None,
                        "name": order_row['customer_name'],
                        "phone": order_row['customer_phone'],
                        "email": order_row['customer_email'],
                    },
                    "served_by_member_id": str(order_row['served_by_member_id']) if order_row['served_by_member_id'] else None,
                    "served_by_member_name": order_row['served_by_member_name'],
                    "items_count": order_row['items_count'],
                    "split_payments": split_payments,
                    "standard_tax": _std_tax,
                    "liquor_tax": _liq_tax,
                    "standard_tax_label": _tax_label,
                }
            }

    except (AuthenticationError, APIError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting order by ID: {str(e)}")
        raise APIError(f"Error getting order by ID: {str(e)}", status_code=500)


async def bulk_update_order_status(
    request: Request,
    order_ids: list,
    status: str,
    payment_method: Optional[str] = None,
    customer_id: Optional[str] = None,
) -> dict:
    """Bulk update status for multiple orders belonging to the tenant."""
    allowed = {"completed", "cancelled", "pending"}
    if status not in allowed:
        raise APIError(f"Estado inválido. Valores permitidos: {', '.join(allowed)}", status_code=400)

    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        from uuid import UUID as _UUID
        ids = [_UUID(oid) for oid in order_ids]

        async with get_db_connection() as conn:
            # Guard: fail fast if any order falls in a closed monthly accounting period (#362)
            closed_check = await conn.fetchrow(
                """
                SELECT 1 FROM orders o
                JOIN tenant_monthly_periods mp
                    ON mp.tenant_id = o.tenant_id
                    AND EXTRACT(YEAR  FROM o.order_date AT TIME ZONE 'America/Bogota') = mp.year
                    AND EXTRACT(MONTH FROM o.order_date AT TIME ZONE 'America/Bogota') = mp.month
                    AND mp.status = 'closed'
                WHERE o.id = ANY($1) AND o.tenant_id = $2
                LIMIT 1
                """,
                ids, tenant_id,
            )
            if closed_check:
                raise APIError(
                    "Una o más órdenes pertenecen a un período contable cerrado. "
                    "Contacta a tu contador para realizar correcciones.",
                    status_code=409,
                )

            # Fetch current state of all orders before updating
            order_rows = await conn.fetch(
                """SELECT id, status, order_number, table_session_id, pos_cart_id, payment_status
                   FROM orders WHERE id = ANY($1) AND tenant_id = $2""",
                ids, tenant_id
            )

            # Block completed → pending for POS orders in bulk
            if status == 'pending':
                blocked = [str(r['id']) for r in order_rows if r['status'] == 'completed' and r['pos_cart_id']]
                if blocked:
                    raise APIError(
                        "Las órdenes completadas del POS no pueden volver a pendiente. Use 'Cancelar' en su lugar.",
                        status_code=400
                    )

            from uuid import UUID as _UUID2
            cid = _UUID2(customer_id) if customer_id else None
            result = await conn.execute(
                """UPDATE orders
                   SET status = $1,
                       payment_method = COALESCE($2, payment_method),
                       customer_id = COALESCE($5, customer_id)
                   WHERE id = ANY($3) AND tenant_id = $4""",
                status, payment_method, ids, tenant_id, cid
            )

            # Stock adjustments and mesa session releases per order
            released_sessions = set()
            for row in order_rows:
                old_status = row['status']
                if old_status == status:
                    continue

                order_id_row = row['id']
                order_number = int(row['order_number'])

                # Stock
                if old_status != 'completed' and status == 'completed':
                    await _deduct_stock_for_status_update(conn, order_id_row, tenant_id, user_id, order_number)
                elif old_status == 'completed' and status in ('cancelled', 'pending'):
                    await _return_stock_for_order_cancellation(conn, order_id_row, tenant_id, user_id, order_number)

                # If cancelling a credit order, clear payment_status so it leaves cartera
                if status == 'cancelled' and row['payment_status'] in ('credit', 'partial'):
                    await conn.execute(
                        "UPDATE orders SET payment_status = NULL WHERE id = $1",
                        order_id_row
                    )

                # Mesa session (deduplicated by session id)
                if status in ('completed', 'cancelled') and row['table_session_id']:
                    sid = row['table_session_id']
                    if sid not in released_sessions:
                        released_sessions.add(sid)
                        await conn.execute(
                            "UPDATE table_sessions SET closed_at = now() WHERE id = $1 AND closed_at IS NULL",
                            sid
                        )
                        await conn.execute(
                            """UPDATE tables SET status = 'free'
                               WHERE id = (SELECT table_id FROM table_sessions WHERE id = $1)
                                 AND tenant_id = $2""",
                            sid, tenant_id
                        )

        updated = int(result.split()[-1])
        return {"success": True, "updated": updated, "message": f"{updated} orden(es) actualizadas a {status}"}

    except (AuthenticationError, APIError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error bulk updating order status: {str(e)}")
        raise APIError(f"Error al actualizar órdenes: {str(e)}", status_code=500)


async def update_order_status(
    request: Request,
    order_id: UUID,
    status: str,
    payment_method: Optional[str] = None,
) -> dict:
    """Update the status of an order (mesa orders only)."""
    allowed = {"completed", "cancelled", "pending", "preparing"}
    if status not in allowed:
        raise APIError(f"Estado inválido. Valores permitidos: {', '.join(allowed)}", status_code=400)

    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            row = await conn.fetchrow(
                """SELECT id, status, order_number, table_session_id, pos_cart_id, payment_status, order_date
                   FROM orders WHERE id = $1 AND tenant_id = $2""",
                order_id, tenant_id
            )
            if not row:
                raise APIError("Orden no encontrada", status_code=404)

            # Guard: block mutation if order falls in a closed monthly accounting period (#362)
            await assert_order_not_in_closed_monthly_period(conn, tenant_id, row['order_date'])

            old_status = row['status']
            order_number = int(row['order_number'])

            # Block completed → pending for POS orders (no active table session to restore)
            if old_status == 'completed' and status == 'pending' and row['pos_cart_id']:
                raise APIError(
                    "Las órdenes completadas del POS no pueden volver a pendiente. Use 'Cancelar' en su lugar.",
                    status_code=400
                )

            await conn.execute(
                """UPDATE orders
                   SET status = $1,
                       payment_method = COALESCE($2, payment_method)
                   WHERE id = $3""",
                status, payment_method, order_id
            )

            # If cancelling a credit order, clear payment_status so it leaves cartera
            if status == 'cancelled' and row['payment_status'] in ('credit', 'partial'):
                await conn.execute(
                    "UPDATE orders SET payment_status = NULL WHERE id = $1",
                    order_id
                )

            # Stock adjustment based on transition
            if old_status != status:
                if old_status != 'completed' and status == 'completed':
                    await _deduct_stock_for_status_update(conn, order_id, tenant_id, user_id, order_number)
                elif old_status == 'completed' and status in ('cancelled', 'pending'):
                    await _return_stock_for_order_cancellation(conn, order_id, tenant_id, user_id, order_number)

            # Release the table session if this is a mesa order being closed
            if status in ("completed", "cancelled") and row['table_session_id']:
                await conn.execute(
                    "UPDATE table_sessions SET closed_at = now() WHERE id = $1 AND closed_at IS NULL",
                    row['table_session_id']
                )
                await conn.execute(
                    """UPDATE tables SET status = 'free'
                       WHERE id = (SELECT table_id FROM table_sessions WHERE id = $1)
                         AND tenant_id = $2""",
                    row['table_session_id'], tenant_id
                )

            # Auto-fire hook for manual orders transitioning to 'preparing'
            if status == "preparing":
                try:
                    # Check if comandas are enabled
                    prof = await conn.fetchrow(
                        "SELECT comandas_enabled FROM tenant_public_profiles WHERE tenant_id = $1",
                        tenant_id
                    )
                    if prof and prof["comandas_enabled"]:
                        # Manual orders are treated as 'delivery' (generic out-of-table order)
                        from app.services.comandas_service import fire_comandas
                        await fire_comandas(
                            order_id=order_id,
                            tenant_id=tenant_id,
                            source_type='delivery',
                            table_display_name='Domicilio',
                            conn=conn
                        )
                except Exception as _fe:
                    logger.error(f"Auto-fire failed for manual order {order_id} (preparing): {_fe}")

        return {"success": True, "message": f"Estado actualizado a {status}"}

    except (AuthenticationError, APIError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error updating order status: {str(e)}")
        raise APIError(f"Error al actualizar estado: {str(e)}", status_code=500)


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
                WHERE id = $1 AND tenant_id = $2
                  AND (pos_cart_id IS NOT NULL OR table_session_id IS NOT NULL OR extra_attributes->>'source' = 'manual')
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
                    oi.discount_allocated,
                    oi.net_total,
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
                        "id": str(mod['id']),  # order_item_modifier ID for deletion
                        "modifier_id": str(mod['modifier_id']) if mod['modifier_id'] else None,
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
                    "discount_allocated": float(item_row['discount_allocated']) if item_row['discount_allocated'] is not None else 0.0,
                    "net_total": float(item_row['net_total']) if item_row['net_total'] is not None else None,
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


async def get_customers_list(
    request: Request,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    payment_method: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 100,
    offset: int = 0
) -> dict:
    """
    Get list of customers aggregated from POS orders.
    Returns customers ranked by total_spent DESC.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            where_conditions = [
                "o.tenant_id = $1",
                "(o.pos_cart_id IS NOT NULL OR o.table_session_id IS NOT NULL OR o.extra_attributes->>'source' = 'manual')",
                "o.customer_id IS NOT NULL",
            ]
            params = [tenant_id]
            param_count = 1

            if payment_method:
                param_count += 1
                where_conditions.append(f"o.payment_method = ${param_count}")
                params.append(payment_method)

            if status:
                param_count += 1
                where_conditions.append(f"o.status = ${param_count}")
                params.append(status)

            parsed_date_from = parse_date(date_from)
            parsed_date_to = parse_date(date_to)

            if parsed_date_from:
                param_count += 1
                where_conditions.append(
                    f"o.order_date >= (${param_count}::timestamp AT TIME ZONE 'America/Bogota')"
                )
                params.append(parsed_date_from)

            if parsed_date_to:
                param_count += 1
                where_conditions.append(
                    f"o.order_date < ((${param_count}::timestamp + interval '1 day') AT TIME ZONE 'America/Bogota')"
                )
                params.append(parsed_date_to)

            if search:
                param_count += 1
                where_conditions.append(
                    f"(p.name ILIKE ${param_count} OR p.phone_number ILIKE ${param_count})"
                )
                params.append(f"%{search}%")

            where_clause = " AND ".join(where_conditions)

            param_count += 1
            limit_param = param_count
            param_count += 1
            offset_param = param_count

            query = f"""
                WITH customer_agg AS (
                    SELECT
                        o.customer_id,
                        COALESCE(p.name, 'Sin identificar') AS name,
                        p.phone_number                       AS phone,
                        SUM(o.total_amount)                  AS total_spent,
                        COUNT(o.id)                          AS order_count,
                        AVG(o.total_amount)                  AS avg_ticket,
                        MAX(o.order_date)                    AS last_order_date
                    FROM orders o
                    LEFT JOIN profile p ON o.customer_id = p.id
                    WHERE {where_clause}
                    GROUP BY o.customer_id, p.name, p.phone_number
                )
                SELECT
                    *,
                    COUNT(*) OVER()          AS total_count,
                    SUM(total_spent) OVER()  AS total_revenue
                FROM customer_agg
                ORDER BY total_spent DESC
                LIMIT ${limit_param} OFFSET ${offset_param}
            """

            params.extend([limit, offset])
            rows = await conn.fetch(query, *params)
            total_count = rows[0]['total_count'] if rows else 0
            total_revenue = float(rows[0]['total_revenue']) if rows else 0.0

            customers = [
                {
                    "customer_id": str(row['customer_id']),
                    "name": row['name'],
                    "phone": row['phone'],
                    "total_spent": float(row['total_spent']),
                    "order_count": int(row['order_count']),
                    "avg_ticket": float(row['avg_ticket']),
                    "last_order_date": row['last_order_date'].isoformat(),
                }
                for row in rows
            ]

            return {
                "success": True,
                "data": customers,
                "total": total_count,
                "total_revenue": total_revenue,
                "limit": limit,
                "offset": offset,
            }

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting customers list: {str(e)}")
        raise APIError(f"Error getting customers list: {str(e)}", status_code=500)


async def get_customer_detail(
    request: Request,
    customer_id: UUID,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page: int = 1,
    per_page: int = 20
) -> dict:
    """
    Get a single customer's aggregate stats plus their paginated POS order history.
    Returns 404 if the customer has no POS orders for this tenant.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # --- Customer aggregate stats ---
            customer_row = await conn.fetchrow(
                """
                SELECT
                    o.customer_id,
                    COALESCE(p.name, 'Sin identificar') AS name,
                    p.phone_number                       AS phone,
                    p.email                              AS email,
                    COUNT(o.id)                          AS total_orders,
                    SUM(o.total_amount)                  AS total_spent,
                    MIN(o.order_date)                    AS first_purchase,
                    MAX(o.order_date)                    AS last_purchase
                FROM orders o
                LEFT JOIN profile p ON o.customer_id = p.id
                WHERE o.tenant_id = $1
                  AND (o.pos_cart_id IS NOT NULL OR o.table_session_id IS NOT NULL OR o.extra_attributes->>'source' = 'manual')
                  AND o.customer_id = $2
                GROUP BY o.customer_id, p.name, p.phone_number, p.email
                """,
                tenant_id,
                customer_id,
            )

            if not customer_row:
                raise APIError("Customer not found", status_code=404)

            # --- Paginated order history ---
            where_conditions = [
                "o.tenant_id = $1",
                "(o.pos_cart_id IS NOT NULL OR o.table_session_id IS NOT NULL OR o.extra_attributes->>'source' = 'manual')",
                "o.customer_id = $2",
            ]
            params = [tenant_id, customer_id]
            param_count = 2

            parsed_date_from = parse_date(date_from)
            parsed_date_to = parse_date(date_to)

            if parsed_date_from:
                param_count += 1
                where_conditions.append(
                    f"o.order_date >= (${param_count}::timestamp AT TIME ZONE 'America/Bogota')"
                )
                params.append(parsed_date_from)

            if parsed_date_to:
                param_count += 1
                where_conditions.append(
                    f"o.order_date < ((${param_count}::timestamp + interval '1 day') AT TIME ZONE 'America/Bogota')"
                )
                params.append(parsed_date_to)

            where_clause = " AND ".join(where_conditions)

            offset = (page - 1) * per_page
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
                    ei.id               AS invoice_id,
                    ei.prefix           AS invoice_prefix,
                    ei.invoice_number   AS invoice_number,
                    ei.status           AS invoice_status,
                    ei.cufe             AS invoice_cufe,
                    COUNT(*) OVER()     AS total_count
                FROM orders o
                LEFT JOIN LATERAL (
                    SELECT id, prefix, invoice_number, status, cufe
                    FROM electronic_invoices
                    WHERE order_id = o.id AND tenant_id = $1
                    ORDER BY created_at DESC LIMIT 1
                ) ei ON true
                WHERE {where_clause}
                ORDER BY o.order_date DESC
                LIMIT ${limit_param} OFFSET ${offset_param}
            """

            params.extend([per_page, offset])
            order_rows = await conn.fetch(orders_query, *params)
            total_orders_in_period = order_rows[0]['total_count'] if order_rows else 0

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
                    "invoice_id": str(row['invoice_id']) if row['invoice_id'] else None,
                    "invoice_prefix": row['invoice_prefix'],
                    "invoice_number": row['invoice_number'],
                    "invoice_status": row['invoice_status'],
                    "invoice_cufe": row['invoice_cufe'],
                }
                for row in order_rows
            ]

            # --- Waros summary (same connection, no extra round-trip) ---
            wallet_row = await conn.fetchrow(
                """
                SELECT current_balance, lifetime_earned, lifetime_spent
                FROM waros_wallets
                WHERE profile_id = $1 AND tenant_id = $2
                """,
                customer_id,
                tenant_id,
            )

            manual_tx_rows = await conn.fetch(
                """
                SELECT id, created_at, waros_amount, description
                FROM waros_transactions
                WHERE profile_id = $1 AND tenant_id = $2
                  AND transaction_type = 'manual'
                ORDER BY created_at DESC
                """,
                customer_id,
                tenant_id,
            )

            waros_summary = {
                "current_balance": int(wallet_row["current_balance"]) if wallet_row else 0,
                "lifetime_earned": int(wallet_row["lifetime_earned"]) if wallet_row else 0,
                "lifetime_spent": int(wallet_row["lifetime_spent"]) if wallet_row else 0,
                "manual_transactions": [
                    {
                        "id": row["id"],
                        "created_at": row["created_at"].isoformat(),
                        "waros_amount": row["waros_amount"],
                        "description": row["description"],
                    }
                    for row in manual_tx_rows
                ],
            }

            return {
                "success": True,
                "customer": {
                    "customer_id": str(customer_row['customer_id']),
                    "name": customer_row['name'],
                    "phone": customer_row['phone'],
                    "email": customer_row['email'],
                    "total_orders": int(customer_row['total_orders']),
                    "total_spent": float(customer_row['total_spent']),
                    "first_purchase": customer_row['first_purchase'].date().isoformat(),
                    "last_purchase": customer_row['last_purchase'].date().isoformat(),
                },
                "orders": {
                    "items": orders,
                    "total": total_orders_in_period,
                    "page": page,
                    "per_page": per_page,
                },
                "waros_summary": waros_summary,
            }

    except (AuthenticationError, APIError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting customer detail {customer_id}: {str(e)}")
        raise APIError(f"Error getting customer detail: {str(e)}", status_code=500)


async def get_orders_metrics(
    request: Request,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    payment_method: Optional[str] = None,
    payment_method_id: Optional[str] = None,
    status: Optional[str] = None
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
            where_conditions = ["tenant_id = $1", "(pos_cart_id IS NOT NULL OR table_session_id IS NOT NULL OR extra_attributes->>'source' = 'manual')"]
            params = [tenant_id]
            param_count = 1

            parsed_date_from = parse_date(date_from)
            parsed_date_to = parse_date(date_to)

            if parsed_date_from:
                param_count += 1
                where_conditions.append(f"order_date >= (${param_count}::timestamp AT TIME ZONE 'America/Bogota')")
                params.append(parsed_date_from)

            if parsed_date_to:
                param_count += 1
                where_conditions.append(f"order_date < ((${param_count}::timestamp + interval '1 day') AT TIME ZONE 'America/Bogota')")
                params.append(parsed_date_to)

            if payment_method:
                param_count += 1
                where_conditions.append(f"payment_method = ${param_count}")
                params.append(payment_method)

            if payment_method_id:
                param_count += 1
                where_conditions.append(f"payment_method_id = ${param_count}::uuid")
                params.append(payment_method_id)

            if status:
                param_count += 1
                where_conditions.append(f"status = ${param_count}")
                params.append(status)

            where_clause = " AND ".join(where_conditions)

            metrics_query = f"""
                SELECT
                    COUNT(*) as total_orders,
                    COUNT(*) FILTER (WHERE status = 'completed') as completed_orders,
                    COUNT(*) FILTER (WHERE status = 'cancelled') as cancelled_orders,
                    COUNT(*) FILTER (WHERE status = 'pending') as pending_orders,
                    COALESCE(SUM(total_amount) FILTER (WHERE status = 'completed'), 0) as total_sales,
                    COALESCE(AVG(total_amount) FILTER (WHERE status = 'completed'), 0) as avg_ticket,
                    COUNT(*) FILTER (WHERE status = 'completed' AND discount_amount > 0) as discount_count,
                    COALESCE(SUM(discount_amount) FILTER (WHERE status = 'completed' AND discount_amount > 0), 0) as total_discount_amount
                FROM orders
                WHERE {where_clause}
            """

            row = await conn.fetchrow(metrics_query, *params)

            # Tax aggregate — sum subtotals by tax_category for completed orders in same date range
            _total_std_tax = 0.0
            _total_liq_tax = 0.0
            _tax_label = "Impuesto"
            try:
                tax_config = await _get_tenant_tax_config(conn, tenant_id)
                # Rebuild WHERE without payment_method_id for tax query (order_items has no that filter)
                tax_where = ["o.tenant_id = $1", "o.status = 'completed'",
                             "(o.pos_cart_id IS NOT NULL OR o.table_session_id IS NOT NULL OR o.extra_attributes->>'source' = 'manual')"]
                tax_params: List[Any] = [tenant_id]
                tax_pc = 1
                if parsed_date_from:
                    tax_pc += 1
                    tax_where.append(f"o.order_date >= (${tax_pc}::timestamp AT TIME ZONE 'America/Bogota')")
                    tax_params.append(parsed_date_from)
                if parsed_date_to:
                    tax_pc += 1
                    tax_where.append(f"o.order_date < ((${tax_pc}::timestamp + interval '1 day') AT TIME ZONE 'America/Bogota')")
                    tax_params.append(parsed_date_to)
                if payment_method:
                    tax_pc += 1
                    tax_where.append(f"o.payment_method = ${tax_pc}")
                    tax_params.append(payment_method)
                if payment_method_id:
                    tax_pc += 1
                    tax_where.append(f"o.payment_method_id = ${tax_pc}::uuid")
                    tax_params.append(payment_method_id)
                tax_where_sql = " AND ".join(tax_where)
                tax_rows = await conn.fetch(
                    f"""SELECT COALESCE(p.tax_category, 'standard') AS tax_category,
                               COALESCE(SUM(oi.subtotal), 0) AS subtotal
                        FROM order_items oi
                        JOIN product p ON p.id = oi.product_id
                        JOIN orders o ON o.id = oi.order_id
                        WHERE {tax_where_sql}
                        GROUP BY COALESCE(p.tax_category, 'standard')""",
                    *tax_params
                )
                _total_std_tax, _total_liq_tax, _tax_label = _compute_tax_breakdown(tax_rows, tax_config)
            except Exception as _e:
                logger.warning(f"Tax metrics computation failed: {_e}")

            return {
                "success": True,
                "data": {
                    "total_sales": float(row['total_sales']),
                    "total_orders": row['total_orders'],
                    "completed_orders": row['completed_orders'],
                    "cancelled_orders": row['cancelled_orders'],
                    "pending_orders": row['pending_orders'],
                    "avg_ticket": float(row['avg_ticket']),
                    "discount_count": int(row['discount_count']),
                    "total_discount_amount": float(row['total_discount_amount']),
                    "total_standard_tax": _total_std_tax,
                    "total_liquor_tax": _total_liq_tax,
                    "standard_tax_label": _tax_label,
                }
            }

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting orders metrics: {str(e)}")
        raise APIError(f"Error getting orders metrics: {str(e)}", status_code=500)


async def get_orders_dashboard(
    request: Request,
    payment_method: Optional[str] = None,
    status: Optional[str] = None
) -> dict:
    """
    Returns all metrics needed for the /ventas dashboard in a single DB query.
    Eliminates the need for 3 separate /orders/metrics calls on page load.

    Returns:
    - main: all-time metrics (filtered by payment_method/status if provided)
    - month: current month-to-date metrics (filtered by payment_method/status if provided)
    - year: current year-to-date metrics (filtered by payment_method/status if provided)
    - commission_savings: main.total_sales * commission_rate (from commission_configs or 30% default)
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Build optional filters for the main (all-time) metrics
            main_filters = []
            params = [tenant_id]
            param_count = 1

            if payment_method:
                param_count += 1
                main_filters.append(f"payment_method = ${param_count}")
                params.append(payment_method)

            if status:
                param_count += 1
                main_filters.append(f"status = ${param_count}")
                params.append(status)

            # Build FILTER clause suffix for main metrics (e.g. "AND payment_method = $2")
            main_filter_sql = ""
            if main_filters:
                main_filter_sql = " AND " + " AND ".join(main_filters)

            dashboard_query = f"""
                SELECT
                    -- Main: all-time (with optional payment/status filters)
                    COUNT(*) FILTER (WHERE status = 'completed'{main_filter_sql}) as main_completed,
                    COALESCE(SUM(total_amount) FILTER (WHERE status = 'completed'{main_filter_sql}), 0) as main_sales,
                    COALESCE(AVG(total_amount) FILTER (WHERE status = 'completed'{main_filter_sql}), 0) as main_avg_ticket,
                    COUNT(*) FILTER (WHERE status = 'completed' AND discount_amount > 0{main_filter_sql}) as main_discount_count,
                    COALESCE(SUM(discount_amount) FILTER (WHERE status = 'completed' AND discount_amount > 0{main_filter_sql}), 0) as main_total_discount,

                    -- Month-to-date (with optional payment/status filters)
                    COUNT(*) FILTER (
                        WHERE status = 'completed'
                        AND DATE(order_date AT TIME ZONE 'America/Bogota') >= DATE_TRUNC('month', NOW() AT TIME ZONE 'America/Bogota')::date
                        {main_filter_sql}
                    ) as month_completed,
                    COALESCE(SUM(total_amount) FILTER (
                        WHERE status = 'completed'
                        AND DATE(order_date AT TIME ZONE 'America/Bogota') >= DATE_TRUNC('month', NOW() AT TIME ZONE 'America/Bogota')::date
                        {main_filter_sql}
                    ), 0) as month_sales,

                    -- Year-to-date (with optional payment/status filters)
                    COUNT(*) FILTER (
                        WHERE status = 'completed'
                        AND DATE(order_date AT TIME ZONE 'America/Bogota') >= DATE_TRUNC('year', NOW() AT TIME ZONE 'America/Bogota')::date
                        {main_filter_sql}
                    ) as year_completed,
                    COALESCE(SUM(total_amount) FILTER (
                        WHERE status = 'completed'
                        AND DATE(order_date AT TIME ZONE 'America/Bogota') >= DATE_TRUNC('year', NOW() AT TIME ZONE 'America/Bogota')::date
                        {main_filter_sql}
                    ), 0) as year_sales

                FROM orders
                WHERE tenant_id = $1 AND (pos_cart_id IS NOT NULL OR table_session_id IS NOT NULL OR extra_attributes->>'source' = 'manual')
            """

            row = await conn.fetchrow(dashboard_query, *params)

            # Fetch commission rate from tenant config (default 30% — typical Rappi/iFood Colombian rate)
            commission_row = await conn.fetchrow(
                "SELECT default_commission_percentage FROM commission_configs WHERE tenant_id = $1",
                tenant_id
            )
            commission_rate = float(commission_row['default_commission_percentage']) if commission_row else 30.0

            main_sales = float(row['main_sales'])
            commission_savings = round(main_sales * (commission_rate / 100))

            # Payment breakdown — UNION ALL: order_payments (split) + legacy orders
            breakdown_rows = await conn.fetch(
                """
                -- Split orders: amounts from order_payments
                SELECT
                    COALESCE(pmg.slug, op.payment_method)  AS group_slug,
                    COALESCE(pmg.name, op.payment_method)  AS group_name,
                    COALESCE(SUM(op.amount), 0)             AS total,
                    COUNT(DISTINCT op.order_id)             AS order_count
                FROM order_payments op
                JOIN orders o ON o.id = op.order_id
                LEFT JOIN payment_methods pm ON pm.id = op.payment_method_id
                LEFT JOIN payment_method_groups pmg ON pmg.id = pm.group_id
                WHERE o.tenant_id = $1
                  AND o.status = 'completed'
                  AND (o.pos_cart_id IS NOT NULL OR o.table_session_id IS NOT NULL OR o.extra_attributes->>'source' = 'manual')
                GROUP BY COALESCE(pmg.slug, op.payment_method), COALESCE(pmg.name, op.payment_method)

                UNION ALL

                -- Legacy orders (no rows in order_payments): use orders.total_amount
                SELECT
                    COALESCE(pmg.slug, o.payment_method)  AS group_slug,
                    COALESCE(pmg.name, o.payment_method)  AS group_name,
                    COALESCE(SUM(o.total_amount), 0)       AS total,
                    COUNT(*)                               AS order_count
                FROM orders o
                LEFT JOIN payment_methods pm ON pm.id = o.payment_method_id
                LEFT JOIN payment_method_groups pmg ON pmg.id = pm.group_id
                WHERE o.tenant_id = $1
                  AND o.status = 'completed'
                  AND (o.pos_cart_id IS NOT NULL OR o.table_session_id IS NOT NULL OR o.extra_attributes->>'source' = 'manual')
                  AND NOT EXISTS (SELECT 1 FROM order_payments op WHERE op.order_id = o.id)
                GROUP BY COALESCE(pmg.slug, o.payment_method), COALESCE(pmg.name, o.payment_method)
                """,
                tenant_id,
            )
            # Aggregate across UNION ALL branches in Python to avoid double-counting
            bd_agg: Dict[str, Any] = {}
            for r in breakdown_rows:
                slug = r["group_slug"]
                if slug is None:
                    continue
                if slug not in bd_agg:
                    bd_agg[slug] = {
                        "group_slug":  slug,
                        "group_name":  r["group_name"],
                        "total":       float(r["total"]),
                        "order_count": int(r["order_count"]),
                    }
                else:
                    entry = bd_agg[slug]
                    entry["total"] = float(entry["total"]) + float(r["total"])
                    entry["order_count"] = int(entry["order_count"]) + int(r["order_count"])
            payment_breakdown = sorted(bd_agg.values(), key=lambda x: x["total"], reverse=True)

            # Tax aggregates — all-time, month-to-date, year-to-date
            _main_std = 0.0
            _main_liq = 0.0
            _month_std = 0.0
            _month_liq = 0.0
            _year_std = 0.0
            _year_liq = 0.0
            _tax_label = "Impuesto"
            _base_filter = "o.tenant_id = $1 AND o.status = 'completed' AND (o.pos_cart_id IS NOT NULL OR o.table_session_id IS NOT NULL OR o.extra_attributes->>'source' = 'manual')"
            _tax_select = """
                SELECT COALESCE(p.tax_category, 'standard') AS tax_category,
                       COALESCE(SUM(oi.subtotal), 0) AS subtotal
                FROM order_items oi
                JOIN product p ON p.id = oi.product_id
                JOIN orders o ON o.id = oi.order_id
            """
            try:
                tax_config = await _get_tenant_tax_config(conn, tenant_id)
                main_tax_rows = await conn.fetch(
                    f"{_tax_select} WHERE {_base_filter} GROUP BY COALESCE(p.tax_category, 'standard')",
                    tenant_id
                )
                month_tax_rows = await conn.fetch(
                    f"""{_tax_select} WHERE {_base_filter}
                        AND DATE(o.order_date AT TIME ZONE 'America/Bogota') >= DATE_TRUNC('month', NOW() AT TIME ZONE 'America/Bogota')::date
                        GROUP BY COALESCE(p.tax_category, 'standard')""",
                    tenant_id
                )
                year_tax_rows = await conn.fetch(
                    f"""{_tax_select} WHERE {_base_filter}
                        AND DATE(o.order_date AT TIME ZONE 'America/Bogota') >= DATE_TRUNC('year', NOW() AT TIME ZONE 'America/Bogota')::date
                        GROUP BY COALESCE(p.tax_category, 'standard')""",
                    tenant_id
                )
                _main_std, _main_liq, _tax_label = _compute_tax_breakdown(main_tax_rows, tax_config)
                _month_std, _month_liq, _ = _compute_tax_breakdown(month_tax_rows, tax_config)
                _year_std, _year_liq, _ = _compute_tax_breakdown(year_tax_rows, tax_config)
            except Exception as _e:
                logger.warning(f"Tax dashboard computation failed: {_e}")

            return {
                "success": True,
                "data": {
                    "main": {
                        "total_sales": main_sales,
                        "completed_orders": row['main_completed'],
                        "avg_ticket": float(row['main_avg_ticket']),
                        "discount_count": int(row['main_discount_count']),
                        "total_discount_amount": float(row['main_total_discount']),
                        "total_standard_tax": _main_std,
                        "total_liquor_tax": _main_liq,
                    },
                    "month": {
                        "total_sales": float(row['month_sales']),
                        "completed_orders": row['month_completed'],
                        "total_standard_tax": _month_std,
                        "total_liquor_tax": _month_liq,
                    },
                    "year": {
                        "total_sales": float(row['year_sales']),
                        "completed_orders": row['year_completed'],
                        "total_standard_tax": _year_std,
                        "total_liquor_tax": _year_liq,
                    },
                    "commission_savings": commission_savings,
                    "commission_rate": commission_rate,
                    "payment_breakdown": payment_breakdown,
                    "standard_tax_label": _tax_label,
                }
            }

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting orders dashboard: {str(e)}")
        raise APIError(f"Error getting orders dashboard: {str(e)}", status_code=500)


async def export_orders_to_email(
    request: Request,
    search: Optional[str] = None,
    search_field: Optional[str] = None,
    payment_method: Optional[str] = None,
    payment_method_id: Optional[str] = None,
    status: Optional[str] = None,
    sort_field: str = "order_date",
    sort_direction: str = "desc",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    tips_only: bool = False,
    member_id: Optional[str] = None,
    channel: Optional[str] = None,
) -> dict:
    """
    Export all orders (without pagination) based on filters and send via email.

    When `tips_only=True` (warocol.com#640), the result is restricted to
    orders with tip_amount > 0 and the CSV emits tip-specific columns
    (channel, mesero, propina, %). Additional filters `member_id` and
    `channel` apply only to this mode.
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
            # Build WHERE clause (same as get_orders_list but without pagination).
            # warocol.com#640 — tips-only mode swaps the orders-list base predicate
            # for tip_amount > 0 (all tips, regardless of channel).
            where_conditions: List[str] = ["o.tenant_id = $1"]
            if tips_only:
                where_conditions.append("o.tip_amount > 0")
            else:
                where_conditions.append(
                    "(o.pos_cart_id IS NOT NULL OR o.table_session_id IS NOT NULL "
                    "OR o.extra_attributes->>'source' = 'manual')"
                )
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

            # Payment method ID filter (specific custom method)
            if payment_method_id:
                param_count += 1
                where_conditions.append(f"o.payment_method_id = ${param_count}::uuid")
                params.append(payment_method_id)

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
                where_conditions.append(f"o.order_date >= (${param_count}::timestamp AT TIME ZONE 'America/Bogota')")
                params.append(parsed_date_from)

            if parsed_date_to:
                param_count += 1
                where_conditions.append(f"o.order_date < ((${param_count}::timestamp + interval '1 day') AT TIME ZONE 'America/Bogota')")
                params.append(parsed_date_to)

            # warocol.com#640 — tips-only filters (ignored when tips_only is False)
            if tips_only and member_id:
                param_count += 1
                where_conditions.append(f"o.served_by_member_id = ${param_count}::uuid")
                params.append(member_id)
            if tips_only and channel:
                if channel == 'online':
                    where_conditions.append("o.online_cart_id IS NOT NULL")
                elif channel == 'mesa':
                    where_conditions.append("o.table_session_id IS NOT NULL")
                elif channel == 'pos':
                    where_conditions.append("o.pos_cart_id IS NOT NULL AND o.table_session_id IS NULL")

            where_clause = " AND ".join(where_conditions)

            # Validate sort field (tips-only mode also accepts tip_amount)
            allowed_sort_fields = ["order_number", "order_date", "total_amount", "customer_name", "payment_method"]
            if tips_only:
                allowed_sort_fields.append("tip_amount")
            if sort_field not in allowed_sort_fields:
                sort_field = "order_date"

            sort_direction = "ASC" if sort_direction.lower() == "asc" else "DESC"

            # Map sort field to actual column
            sort_column_map = {
                "order_number": "o.order_number",
                "order_date": "o.order_date",
                "total_amount": "o.total_amount",
                "tip_amount": "o.tip_amount",
                "customer_name": "p.name",
                "payment_method": "o.payment_method"
            }
            sort_column = sort_column_map.get(sort_field, "o.order_date")

            # Get ALL rows without pagination. warocol.com#640 — tips-only mode
            # adds tip_amount/tip_source + mesero name + cart linkage so the CSV
            # can render channel and the tip-specific columns.
            if tips_only:
                orders_query = f"""
                    SELECT
                        o.id,
                        o.order_number,
                        o.order_date,
                        o.total_amount,
                        o.tip_amount,
                        o.tip_source,
                        o.status,
                        o.payment_method,
                        COALESCE(pmg.name, o.payment_method) AS payment_method_display,
                        p.name AS member_name,
                        o.online_cart_id,
                        o.table_session_id,
                        o.pos_cart_id
                    FROM orders o
                    LEFT JOIN tenant_members tm ON tm.id = o.served_by_member_id
                    LEFT JOIN profile p ON p.id = tm.user_id
                    LEFT JOIN payment_methods pm ON pm.id = o.payment_method_id AND pm.tenant_id = $1
                    LEFT JOIN payment_method_groups pmg ON pmg.id = pm.group_id AND pmg.tenant_id = $1
                    WHERE {where_clause}
                    ORDER BY {sort_column} {sort_direction}
                """
            else:
                orders_query = f"""
                    SELECT
                        o.id,
                        o.order_number,
                        o.order_date,
                        o.total_amount,
                        o.status,
                        o.payment_method,
                        COALESCE(pmg.name, o.payment_method) AS payment_method_display,
                        p.name as customer_name,
                        p.phone_number as customer_phone,
                        (
                            SELECT COUNT(*)
                            FROM order_items oi
                            WHERE oi.order_id = o.id
                        ) as items_count
                    FROM orders o
                    LEFT JOIN profile p ON o.customer_id = p.id
                    LEFT JOIN payment_methods pm ON pm.id = o.payment_method_id AND pm.tenant_id = $1
                    LEFT JOIN payment_method_groups pmg ON pmg.id = pm.group_id AND pmg.tenant_id = $1
                    WHERE {where_clause}
                    ORDER BY {sort_column} {sort_direction}
                """

            orders_rows = await conn.fetch(orders_query, *params)

            if not orders_rows:
                noun = "propinas" if tips_only else "ventas"
                raise APIError(f"No hay {noun} para exportar con los filtros seleccionados", status_code=404)

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

            total_sum = 0  # sum of total_amount (orders) or tip_amount (tips_only)
            completed_count = 0
            cancelled_count = 0

            if tips_only:
                # warocol.com#640 — tip-specific CSV shape
                writer.writerow([
                    'Numero Orden',
                    'Fecha',
                    'Hora',
                    'Canal',
                    'Mesero',
                    'Total Orden',
                    'Propina',
                    'Porcentaje',
                    'Metodo Pago',
                ])
                for row in orders_rows:
                    order_date_str = row['order_date'].strftime('%Y-%m-%d') if row['order_date'] else ''
                    order_time_str = row['order_date'].strftime('%H:%M:%S') if row['order_date'] else ''
                    total_amount = float(row['total_amount'])
                    tip_amount = float(row['tip_amount'])
                    pct = round(tip_amount / total_amount * 100, 2) if total_amount > 0 else 0
                    if row['online_cart_id']:
                        ch = 'online'
                    elif row['table_session_id']:
                        ch = 'mesa'
                    else:
                        ch = 'pos'
                    total_sum += tip_amount
                    completed_count += 1
                    writer.writerow([
                        row['order_number'],
                        order_date_str,
                        order_time_str,
                        ch,
                        row['member_name'] or 'Sin asignar',
                        total_amount,
                        tip_amount,
                        f"{pct}%",
                        row['payment_method_display'] or row['payment_method'] or '',
                    ])
            else:
                # Standard orders export (unchanged shape)
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
                for row in orders_rows:
                    order_date_str = row['order_date'].strftime('%Y-%m-%d') if row['order_date'] else ''
                    order_time_str = row['order_date'].strftime('%H:%M:%S') if row['order_date'] else ''
                    total_amount = float(row['total_amount'])

                    if row['status'] == 'completed':
                        total_sum += total_amount
                        completed_count += 1
                    elif row['status'] == 'cancelled':
                        cancelled_count += 1

                    writer.writerow([
                        row['order_number'],
                        order_date_str,
                        order_time_str,
                        row['customer_name'] or 'Sin nombre',
                        row['customer_phone'] or '',
                        row['items_count'],
                        row['payment_method_display'] or row['payment_method'] or '',
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
                filter_desc.append(f"Método de pago: {payment_method}")
            if search:
                filter_desc.append(f"Búsqueda: {search}")

            filter_text = "\n".join(filter_desc) if filter_desc else "Sin filtros aplicados"

            date_str = now.strftime('%Y-%m-%d_%H%M')

            if tips_only:
                # warocol.com#640 — tip-specific email copy + filename + subject
                email_body = f"""¡Hola {user_name}!

Aquí está tu reporte de propinas solicitado.

RESUMEN
-------
Total de propinas exportadas: {len(orders_rows)}
Suma total de propinas: ${total_sum:,.0f}
Fecha de generación: {now.strftime('%d/%m/%Y %H:%M')}

FILTROS APLICADOS
-----------------
{filter_text}

Adjunto encontrarás el archivo CSV con el detalle de las propinas.
Puedes abrirlo con Excel o Google Sheets.

---
Saifer 101 de Waro Colombia
Tecnología colombiana para el mundo.
"""
                filename = f"propinas_{date_str}.csv"
                subject = f"Reporte de Propinas - {date_str}"
            else:
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
                filename = f"ventas_{date_str}.csv"
                subject = f"Reporte de Ventas - {date_str}"

            # Send email with CSV attachment
            success = await ses_service.send_email_with_attachment(
                from_email="hola@warocol.com",
                from_name="Waro Colombia - Reportes",
                to_emails=[user_email],
                subject=subject,
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


async def delete_order_item(
    request: Request,
    order_id: UUID,
    item_id: UUID
) -> dict:
    """
    Delete an order item and its associated modifiers.
    Returns ingredients to inventory (reverse of POS consumption).
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                # Verify order exists and get order number
                order_query = """
                    SELECT id, order_number, order_date FROM orders
                    WHERE id = $1 AND tenant_id = $2 AND pos_cart_id IS NOT NULL
                """
                order_row = await conn.fetchrow(order_query, order_id, tenant_id)

                if not order_row:
                    raise APIError("Order not found", status_code=404)

                # Guard: block mutation if order falls in a closed monthly accounting period (#362)
                await assert_order_not_in_closed_monthly_period(conn, tenant_id, order_row['order_date'])

                order_number = order_row['order_number']

                # Get item details before deletion
                item_query = """
                    SELECT oi.id, oi.product_id, oi.quantity, p.name as product_name
                    FROM order_items oi
                    JOIN product p ON oi.product_id = p.id
                    WHERE oi.id = $1 AND oi.order_id = $2
                """
                item_row = await conn.fetchrow(item_query, item_id, order_id)

                if not item_row:
                    raise APIError("Order item not found", status_code=404)

                # Check if this is the last item - don't allow deletion
                items_count_query = """
                    SELECT COUNT(*) as count FROM order_items WHERE order_id = $1
                """
                items_count_row = await conn.fetchrow(items_count_query, order_id)
                if items_count_row['count'] <= 1:
                    raise APIError("No se puede eliminar el único producto de la venta", status_code=400)

                product_id = item_row['product_id']
                item_quantity = float(item_row['quantity'])
                product_name = item_row['product_name']

                # 1. Return ingredients from product recipes + base recipes
                ingredients_query = """
                    -- Direct product ingredients
                    SELECT
                        pr.ingredient_id,
                        pr.quantity,
                        pr.unit,
                        i.name as ingredient_name
                    FROM product_recipes pr
                    JOIN ingredients i ON pr.ingredient_id = i.id
                    WHERE pr.product_id = $1

                    UNION ALL

                    -- Ingredients from recipe bases (Issue #517: multiply by pbr.quantity)
                    SELECT
                        brt.ingredient_id,
                        brt.base_quantity * pbr.quantity as quantity,
                        brt.unit,
                        i.name as ingredient_name
                    FROM product_base_recipes pbr
                    JOIN base_recipe_templates brt ON pbr.product_base_type_id = brt.product_base_type_id
                    JOIN ingredients i ON brt.ingredient_id = i.id
                    WHERE pbr.product_id = $1
                """
                ingredients = await conn.fetch(ingredients_query, product_id)

                for ingredient in ingredients:
                    quantity_to_return = item_quantity * float(ingredient['quantity'])
                    await _return_ingredient_to_stock(
                        conn, tenant_id, user_id, order_id, order_number,
                        ingredient['ingredient_id'],
                        quantity_to_return,
                        ingredient['unit'],
                        ingredient['ingredient_name'],
                        f"Devolución por eliminación de {int(item_quantity)}x {product_name}"
                    )

                # 2. Return ingredients from modifiers
                modifiers_query = """
                    SELECT
                        oim.modifier_id,
                        oim.modifier_name,
                        oim.quantity as modifier_qty,
                        m.ingredient_id,
                        m.ingredient_quantity,
                        m.ingredient_unit,
                        i.name as ingredient_name
                    FROM order_item_modifiers oim
                    LEFT JOIN modifiers m ON oim.modifier_id = m.id
                    LEFT JOIN ingredients i ON m.ingredient_id = i.id
                    WHERE oim.order_item_id = $1
                """
                modifiers = await conn.fetch(modifiers_query, item_id)

                for modifier in modifiers:
                    if modifier['ingredient_id'] and modifier['ingredient_quantity']:
                        modifier_qty = float(modifier['modifier_qty']) if modifier['modifier_qty'] else 1.0
                        quantity_to_return = item_quantity * modifier_qty * float(modifier['ingredient_quantity'])
                        await _return_ingredient_to_stock(
                            conn, tenant_id, user_id, order_id, order_number,
                            modifier['ingredient_id'],
                            quantity_to_return,
                            modifier['ingredient_unit'] or 'und',
                            modifier['ingredient_name'],
                            f"Devolución modificador {modifier['modifier_name']} de {product_name}"
                        )

                # 3. Delete associated modifiers (foreign key constraint)
                await conn.execute(
                    "DELETE FROM order_item_modifiers WHERE order_item_id = $1",
                    item_id
                )

                # 4. Delete the order item
                await conn.execute(
                    "DELETE FROM order_items WHERE id = $1",
                    item_id
                )

                # 5. Update order total
                new_total_query = """
                    SELECT COALESCE(SUM(subtotal), 0) as new_total
                    FROM order_items
                    WHERE order_id = $1
                """
                new_total_row = await conn.fetchrow(new_total_query, order_id)
                new_total = float(new_total_row['new_total'])

                await conn.execute(
                    "UPDATE orders SET total_amount = $1 WHERE id = $2",
                    new_total, order_id
                )

                logger.info(f"Order item deleted and inventory restored for Order #{order_number}")

                return {
                    "success": True,
                    "message": "Item eliminado y stock actualizado",
                    "data": {
                        "new_total": new_total
                    }
                }

    except (AuthenticationError, APIError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error deleting order item: {str(e)}")
        raise APIError(f"Error deleting order item: {str(e)}", status_code=500)


_INGREDIENTS_QUERY = """
    SELECT pr.ingredient_id, pr.quantity, pr.unit, i.name AS ingredient_name
    FROM product_recipes pr
    JOIN ingredients i ON pr.ingredient_id = i.id
    WHERE pr.product_id = $1
    UNION ALL
    -- Issue #517: multiply by pbr.quantity
    SELECT brt.ingredient_id, brt.base_quantity * pbr.quantity AS quantity, brt.unit, i.name AS ingredient_name
    FROM product_base_recipes pbr
    JOIN base_recipe_templates brt ON pbr.product_base_type_id = brt.product_base_type_id
    JOIN ingredients i ON brt.ingredient_id = i.id
    WHERE pbr.product_id = $1
"""


async def _deduct_stock_for_status_update(conn, order_id, tenant_id, user_id, order_number: int) -> None:
    """Deduct ingredient stock when an order transitions to completed via status update."""
    items = await conn.fetch(
        """SELECT oi.product_id, oi.quantity, p.name AS product_name
           FROM order_items oi
           JOIN product p ON p.id = oi.product_id
           WHERE oi.order_id = $1""",
        order_id,
    )
    for item in items:
        ingredients = await conn.fetch(_INGREDIENTS_QUERY, item["product_id"])
        for ing in ingredients:
            qty = float(item["quantity"]) * float(ing["quantity"])
            stock_row = await conn.fetchrow(
                "SELECT current_stock FROM tenant_inventory WHERE ingredient_id = $1 AND tenant_id = $2 FOR UPDATE",
                ing["ingredient_id"], tenant_id,
            )
            prev = float(stock_row["current_stock"]) if stock_row else 0.0
            new = prev - qty
            if stock_row:
                await conn.execute(
                    "UPDATE tenant_inventory SET current_stock = $1, last_updated = NOW() WHERE ingredient_id = $2 AND tenant_id = $3",
                    new, ing["ingredient_id"], tenant_id,
                )
            else:
                await conn.execute(
                    "INSERT INTO tenant_inventory (tenant_id, ingredient_id, current_stock, minimum_stock, last_updated) VALUES ($1, $2, $3, 0, NOW())",
                    tenant_id, ing["ingredient_id"], -qty,
                )
            await conn.execute(
                """INSERT INTO tenant_ingredient_movements (
                    tenant_id, ingredient_id, movement_type, quantity_change, unit,
                    previous_stock, new_stock, reference_table, reference_id, reason, created_by, created_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,NOW())""",
                tenant_id, ing["ingredient_id"], "consumption", -qty,
                ing["unit"], prev, new, "orders", order_id,
                f"Venta #{order_number} completada: {item['quantity']}x {item['product_name']}",
                user_id,
            )
            logger.info(f"Stock deducted (status update): {ing['ingredient_name']} -{qty}{ing['unit']} (Orden #{order_number})")


async def _return_stock_for_order_cancellation(conn, order_id, tenant_id, user_id, order_number: int) -> None:
    """Return ingredient stock when a completed order is cancelled or rolled back to pending."""
    items = await conn.fetch(
        """SELECT oi.product_id, oi.quantity, p.name AS product_name
           FROM order_items oi
           JOIN product p ON p.id = oi.product_id
           WHERE oi.order_id = $1""",
        order_id,
    )
    for item in items:
        ingredients = await conn.fetch(_INGREDIENTS_QUERY, item["product_id"])
        for ing in ingredients:
            qty = float(item["quantity"]) * float(ing["quantity"])
            await _return_ingredient_to_stock(
                conn, tenant_id, user_id, order_id, order_number,
                ing["ingredient_id"], qty, ing["unit"], ing["ingredient_name"],
                f"Cancelación: {item['quantity']}x {item['product_name']}"
            )
            logger.info(f"Stock returned (cancellation): {ing['ingredient_name']} +{qty}{ing['unit']} (Orden #{order_number})")


async def _return_ingredient_to_stock(
    conn, tenant_id, user_id, order_id, order_number,
    ingredient_id, quantity, unit, ingredient_name, reason_detail
):
    """
    Helper function to return an ingredient to stock and create movement record.
    """
    # Get current stock
    stock_row = await conn.fetchrow(
        """
        SELECT current_stock FROM tenant_inventory
        WHERE ingredient_id = $1 AND tenant_id = $2
        FOR UPDATE
        """,
        ingredient_id, tenant_id
    )

    previous_stock = float(stock_row['current_stock']) if stock_row else 0.0
    new_stock = previous_stock + quantity

    # Update or create inventory record
    if stock_row:
        await conn.execute(
            """
            UPDATE tenant_inventory
            SET current_stock = $1, last_updated = NOW()
            WHERE ingredient_id = $2 AND tenant_id = $3
            """,
            new_stock, ingredient_id, tenant_id
        )
    else:
        await conn.execute(
            """
            INSERT INTO tenant_inventory (
                tenant_id, ingredient_id, current_stock, minimum_stock, last_updated
            ) VALUES ($1, $2, $3, 0, NOW())
            """,
            tenant_id, ingredient_id, quantity
        )

    # Create movement record
    await conn.execute(
        """
        INSERT INTO tenant_ingredient_movements (
            tenant_id, ingredient_id, movement_type,
            quantity_change, unit, previous_stock, new_stock,
            reference_table, reference_id, reason, created_by, created_at
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW())
        """,
        tenant_id,
        ingredient_id,
        'return',  # New movement type for returns
        quantity,  # Positive = adding back to stock
        unit,
        previous_stock,
        new_stock,
        'orders',
        order_id,
        f"Ajuste Venta #{order_number}: {reason_detail} ({ingredient_name})",
        user_id
    )

    logger.info(f"Inventory restored: {ingredient_name} +{quantity}{unit} (Order #{order_number})")


async def delete_order_item_modifier(
    request: Request,
    order_id: UUID,
    item_id: UUID,
    modifier_id: UUID
) -> dict:
    """
    Delete a modifier from an order item.
    Returns ingredient to inventory if modifier has linked ingredient.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                # Verify order exists and get order number
                order_query = """
                    SELECT id, order_number, order_date FROM orders
                    WHERE id = $1 AND tenant_id = $2 AND pos_cart_id IS NOT NULL
                """
                order_row = await conn.fetchrow(order_query, order_id, tenant_id)

                if not order_row:
                    raise APIError("Order not found", status_code=404)

                # Guard: block mutation if order falls in a closed monthly accounting period (#362)
                await assert_order_not_in_closed_monthly_period(conn, tenant_id, order_row['order_date'])

                order_number = order_row['order_number']

                # Get item details
                item_query = """
                    SELECT oi.id, oi.quantity, p.name as product_name
                    FROM order_items oi
                    JOIN product p ON oi.product_id = p.id
                    WHERE oi.id = $1 AND oi.order_id = $2
                """
                item_row = await conn.fetchrow(item_query, item_id, order_id)

                if not item_row:
                    raise APIError("Order item not found", status_code=404)

                item_quantity = float(item_row['quantity'])
                product_name = item_row['product_name']

                # Get modifier details with ingredient info
                modifier_query = """
                    SELECT
                        oim.id,
                        oim.price_at_purchase,
                        oim.modifier_name,
                        oim.quantity as modifier_qty,
                        oim.modifier_id as original_modifier_id,
                        m.ingredient_id,
                        m.ingredient_quantity,
                        m.ingredient_unit,
                        i.name as ingredient_name
                    FROM order_item_modifiers oim
                    LEFT JOIN modifiers m ON oim.modifier_id = m.id
                    LEFT JOIN ingredients i ON m.ingredient_id = i.id
                    WHERE oim.id = $1 AND oim.order_item_id = $2
                """
                modifier_row = await conn.fetchrow(modifier_query, modifier_id, item_id)

                if not modifier_row:
                    raise APIError("Modifier not found", status_code=404)

                modifier_name = modifier_row['modifier_name']

                # Return ingredient to stock if modifier has linked ingredient
                if modifier_row['ingredient_id'] and modifier_row['ingredient_quantity']:
                    modifier_qty = float(modifier_row['modifier_qty']) if modifier_row['modifier_qty'] else 1.0
                    quantity_to_return = item_quantity * modifier_qty * float(modifier_row['ingredient_quantity'])

                    await _return_ingredient_to_stock(
                        conn, tenant_id, user_id, order_id, order_number,
                        modifier_row['ingredient_id'],
                        quantity_to_return,
                        modifier_row['ingredient_unit'] or 'und',
                        modifier_row['ingredient_name'],
                        f"Devolución modificador {modifier_name} de {product_name}"
                    )

                # Delete the modifier
                await conn.execute(
                    "DELETE FROM order_item_modifiers WHERE id = $1",
                    modifier_id
                )

                # Update item subtotal
                new_item_subtotal_query = """
                    SELECT
                        oi.price_at_purchase * oi.quantity +
                        COALESCE(SUM(oim.price_at_purchase * oi.quantity), 0) as new_subtotal
                    FROM order_items oi
                    LEFT JOIN order_item_modifiers oim ON oim.order_item_id = oi.id
                    WHERE oi.id = $1
                    GROUP BY oi.id, oi.price_at_purchase, oi.quantity
                """
                new_subtotal_row = await conn.fetchrow(new_item_subtotal_query, item_id)
                new_item_subtotal = float(new_subtotal_row['new_subtotal']) if new_subtotal_row else 0

                await conn.execute(
                    "UPDATE order_items SET subtotal = $1 WHERE id = $2",
                    new_item_subtotal, item_id
                )

                # Update order total
                new_total_query = """
                    SELECT COALESCE(SUM(subtotal), 0) as new_total
                    FROM order_items
                    WHERE order_id = $1
                """
                new_total_row = await conn.fetchrow(new_total_query, order_id)
                new_total = float(new_total_row['new_total'])

                await conn.execute(
                    "UPDATE orders SET total_amount = $1 WHERE id = $2",
                    new_total, order_id
                )

                logger.info(f"Modifier {modifier_name} deleted from Order #{order_number}")

                return {
                    "success": True,
                    "message": "Modificador eliminado y stock actualizado",
                    "data": {
                        "new_item_subtotal": new_item_subtotal,
                        "new_total": new_total
                    }
                }

    except (AuthenticationError, APIError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error deleting order item modifier: {str(e)}")
        raise APIError(f"Error deleting order item modifier: {str(e)}", status_code=500)


async def get_sales_flow(
    request: Request,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    payment_method: Optional[str] = None,
    status: Optional[str] = None
) -> dict:
    """
    Get sales flow data with intelligent comparison and grouping
    - Ranges ≤30 days: Compare with previous period
    - Ranges >30 days: Compare with same period last year
    - Auto-grouping: hourly (≤3 days), daily (4-90 days)
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        # Parse dates and calculate periods
        from datetime import datetime, timedelta

        parsed_date_from = parse_date(date_from)
        parsed_date_to = parse_date(date_to)

        # Default to year-to-date if no dates provided (same as metrics cards)
        if not parsed_date_from or not parsed_date_to:
            today = datetime.now().date()
            parsed_date_to = today
            # Start from January 1st of current year
            parsed_date_from = today.replace(month=1, day=1)

        # Calculate range duration
        days_diff = (parsed_date_to - parsed_date_from).days + 1

        # Determine comparison period and grouping
        if days_diff <= 30:
            # Compare with previous period
            comparison_date_to = parsed_date_from - timedelta(days=1)
            comparison_date_from = comparison_date_to - timedelta(days=days_diff - 1)
            comparison_label = "Período Anterior"
        else:
            # Compare with same period last year
            comparison_date_from = parsed_date_from.replace(year=parsed_date_from.year - 1)
            comparison_date_to = parsed_date_to.replace(year=parsed_date_to.year - 1)
            comparison_label = "Año Anterior"

        # Determine grouping: hourly (≤3 days), daily (>3 days)
        group_by = 'hour' if days_diff <= 3 else 'day'

        async with get_db_connection() as conn:
            # Build WHERE conditions
            where_conditions = ["tenant_id = $1", "(pos_cart_id IS NOT NULL OR table_session_id IS NOT NULL OR extra_attributes->>'source' = 'manual')"]
            params = [tenant_id]
            param_count = 1

            # Add filters
            if payment_method:
                param_count += 1
                where_conditions.append(f"payment_method = ${param_count}")
                params.append(payment_method)

            if status:
                param_count += 1
                where_conditions.append(f"status = ${param_count}")
                params.append(status)
            else:
                # Default to completed if no status filter
                where_conditions.append("status = 'completed'")

            where_clause = " AND ".join(where_conditions)

            # Build query based on grouping
            if group_by == 'hour':
                # Hourly grouping
                param_count += 1
                date_from_param_idx = param_count
                param_count += 1
                date_to_param_idx = param_count
                param_count += 1
                comp_from_param_idx = param_count
                param_count += 1
                comp_to_param_idx = param_count

                params.extend([parsed_date_from, parsed_date_to, comparison_date_from, comparison_date_to])

                query = f"""
                    WITH hours AS (
                        SELECT generate_series(0, 23) AS hour
                    ),
                    current_period AS (
                        SELECT
                            EXTRACT(HOUR FROM order_date AT TIME ZONE 'America/Bogota') AS hour,
                            SUM(total_amount) AS sales
                        FROM orders
                        WHERE {where_clause}
                          AND DATE(order_date AT TIME ZONE 'America/Bogota') >= ${date_from_param_idx}
                          AND DATE(order_date AT TIME ZONE 'America/Bogota') <= ${date_to_param_idx}
                        GROUP BY EXTRACT(HOUR FROM order_date AT TIME ZONE 'America/Bogota')
                    ),
                    comparison_period AS (
                        SELECT
                            EXTRACT(HOUR FROM order_date AT TIME ZONE 'America/Bogota') AS hour,
                            SUM(total_amount) AS sales
                        FROM orders
                        WHERE {where_clause}
                          AND DATE(order_date AT TIME ZONE 'America/Bogota') >= ${comp_from_param_idx}
                          AND DATE(order_date AT TIME ZONE 'America/Bogota') <= ${comp_to_param_idx}
                        GROUP BY EXTRACT(HOUR FROM order_date AT TIME ZONE 'America/Bogota')
                    )
                    SELECT
                        h.hour,
                        COALESCE(cp.sales, 0) AS current_sales,
                        COALESCE(cmp.sales, 0) AS comparison_sales
                    FROM hours h
                    LEFT JOIN current_period cp ON h.hour = cp.hour
                    LEFT JOIN comparison_period cmp ON h.hour = cmp.hour
                    WHERE COALESCE(cp.sales, 0) > 0 OR COALESCE(cmp.sales, 0) > 0
                    ORDER BY h.hour
                """

                rows = await conn.fetch(query, *params)

                # Format data for hourly
                data = []
                for row in rows:
                    hour = int(row['hour'])
                    if hour == 0:
                        label = '12am'
                    elif hour < 12:
                        label = f'{hour}am'
                    elif hour == 12:
                        label = '12pm'
                    else:
                        label = f'{hour-12}pm'

                    data.append({
                        'name': label,
                        'sales': round(float(row['current_sales'])),
                        'salesYesterday': round(float(row['comparison_sales']))
                    })

            else:  # day grouping
                # Daily grouping - optimized without recursive CTEs
                param_count += 1
                date_from_param_idx = param_count
                param_count += 1
                date_to_param_idx = param_count
                param_count += 1
                comp_from_param_idx = param_count
                param_count += 1
                comp_to_param_idx = param_count

                params.extend([parsed_date_from, parsed_date_to, comparison_date_from, comparison_date_to])

                # Use generate_series directly in the query (more efficient than recursive CTE)
                query = f"""
                    WITH date_series AS (
                        SELECT generate_series(
                            ${date_from_param_idx}::date,
                            ${date_to_param_idx}::date,
                            '1 day'::interval
                        )::date AS day
                    ),
                    current_period AS (
                        SELECT
                            DATE(order_date AT TIME ZONE 'America/Bogota') AS day,
                            SUM(total_amount) AS sales
                        FROM orders
                        WHERE {where_clause}
                          AND DATE(order_date AT TIME ZONE 'America/Bogota') >= ${date_from_param_idx}
                          AND DATE(order_date AT TIME ZONE 'America/Bogota') <= ${date_to_param_idx}
                        GROUP BY DATE(order_date AT TIME ZONE 'America/Bogota')
                    ),
                    comparison_period AS (
                        SELECT
                            DATE(order_date AT TIME ZONE 'America/Bogota') AS day,
                            SUM(total_amount) AS sales
                        FROM orders
                        WHERE {where_clause}
                          AND DATE(order_date AT TIME ZONE 'America/Bogota') >= ${comp_from_param_idx}
                          AND DATE(order_date AT TIME ZONE 'America/Bogota') <= ${comp_to_param_idx}
                        GROUP BY DATE(order_date AT TIME ZONE 'America/Bogota')
                    )
                    SELECT
                        ds.day,
                        COALESCE(cp.sales, 0) AS current_sales,
                        COALESCE(cmp.sales, 0) AS comparison_sales
                    FROM date_series ds
                    LEFT JOIN current_period cp ON ds.day = cp.day
                    LEFT JOIN comparison_period cmp ON
                        cmp.day = (ds.day - (${date_from_param_idx}::date - ${comp_from_param_idx}::date))
                    ORDER BY ds.day
                """

                rows = await conn.fetch(query, *params)

                # Format data for daily
                data = []
                for row in rows:
                    day = row['day']
                    label = day.strftime('%d/%m')

                    data.append({
                        'name': label,
                        'sales': round(float(row['current_sales'])),
                        'salesYesterday': round(float(row['comparison_sales']))
                    })

            # If no data, return placeholder
            if not data:
                data = [
                    { 'name': '12pm', 'sales': 0, 'salesYesterday': 0 },
                    { 'name': '2pm', 'sales': 0, 'salesYesterday': 0 },
                    { 'name': '4pm', 'sales': 0, 'salesYesterday': 0 },
                    { 'name': '6pm', 'sales': 0, 'salesYesterday': 0 },
                    { 'name': '8pm', 'sales': 0, 'salesYesterday': 0 },
                    { 'name': '10pm', 'sales': 0, 'salesYesterday': 0 },
                ]

            return {
                "success": True,
                "data": data,
                "metadata": {
                    "grouping": group_by,
                    "comparison_type": "previous_period" if days_diff <= 30 else "year_over_year",
                    "comparison_label": comparison_label,
                    "days_in_range": days_diff
                }
            }

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting sales flow: {str(e)}")
        raise APIError(f"Error getting sales flow: {str(e)}", status_code=500)


async def create_manual_order(
    request: Request,
    order_date: str,
    payment_method: str,
    items: List[dict],
    customer_id: Optional[str] = None,
    payment_method_id: Optional[str] = None,
) -> dict:
    """
    Create an order manually with a custom date, bypassing the POS cart.
    Stores extra_attributes = {"source": "manual"} to identify it.
    """
    import json
    from datetime import datetime

    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        if not items:
            raise APIError("At least one item is required", status_code=400)

        try:
            order_datetime = datetime.fromisoformat(order_date)
        except ValueError:
            raise APIError("Invalid order_date format. Expected YYYY-MM-DDTHH:MM", status_code=400)

        async with get_db_connection() as conn:
            async with conn.transaction():
                # Guard: block creation if order_date falls in a closed monthly accounting period (#362)
                await assert_order_not_in_closed_monthly_period(conn, tenant_id, order_datetime)

                # Compute total server-side — never trust client total
                total_amount = sum(
                    float(item["quantity"]) * float(item["unit_price"])
                    + sum(float(m.get("price", 0)) for m in item.get("modifiers", []))
                    for item in items
                )

                order_row = await conn.fetchrow(
                    """
                    INSERT INTO orders (
                        tenant_id, customer_id, payment_method, payment_method_id,
                        order_date, total_amount, status, extra_attributes
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, 'completed', $7)
                    RETURNING id, order_number, order_date, created_at
                    """,
                    tenant_id,
                    UUID(customer_id) if customer_id else None,
                    payment_method,
                    UUID(payment_method_id) if payment_method_id else None,
                    order_datetime,
                    total_amount,
                    json.dumps({"source": "manual"})
                )

                order_id = order_row["id"]

                for item in items:
                    modifiers = item.get("modifiers", [])
                    modifiers_total = sum(float(m.get("price", 0)) for m in modifiers)
                    subtotal = float(item["quantity"]) * float(item["unit_price"]) + modifiers_total
                    order_item_row = await conn.fetchrow(
                        """
                        INSERT INTO order_items (
                            order_id, product_id, quantity, price_at_purchase, subtotal
                        )
                        VALUES ($1, $2, $3, $4, $5)
                        RETURNING id
                        """,
                        order_id,
                        UUID(item["product_id"]),
                        float(item["quantity"]),
                        float(item["unit_price"]),
                        subtotal
                    )
                    order_item_id = order_item_row["id"]

                    for modifier in modifiers:
                        await conn.execute(
                            """
                            INSERT INTO order_item_modifiers (
                                order_item_id, modifier_id, modifier_name,
                                price_at_purchase, quantity
                            )
                            VALUES ($1, $2, $3, $4, 1)
                            """,
                            order_item_id,
                            UUID(modifier["id"]),
                            modifier["name"],
                            float(modifier.get("price", 0))
                        )

                        # Deduct inventory for modifier ingredient (if linked)
                        modifier_ingredient = await conn.fetchrow(
                            """
                            SELECT
                                m.ingredient_id,
                                m.ingredient_quantity,
                                m.ingredient_unit,
                                i.name AS ingredient_name
                            FROM modifiers m
                            LEFT JOIN ingredients i ON m.ingredient_id = i.id
                            WHERE m.id = $1 AND m.ingredient_id IS NOT NULL
                            """,
                            UUID(modifier["id"])
                        )

                        if modifier_ingredient and modifier_ingredient["ingredient_id"] and modifier_ingredient["ingredient_quantity"]:
                            total_deduction = float(item["quantity"]) * float(modifier_ingredient["ingredient_quantity"])
                            stock_row = await conn.fetchrow(
                                "SELECT current_stock FROM tenant_inventory WHERE ingredient_id = $1 AND tenant_id = $2",
                                modifier_ingredient["ingredient_id"],
                                tenant_id
                            )
                            previous_stock = float(stock_row["current_stock"]) if stock_row else 0.0
                            new_stock = previous_stock - total_deduction

                            if stock_row:
                                await conn.execute(
                                    "UPDATE tenant_inventory SET current_stock = $1, last_updated = NOW() WHERE ingredient_id = $2 AND tenant_id = $3",
                                    new_stock, modifier_ingredient["ingredient_id"], tenant_id
                                )
                            else:
                                await conn.execute(
                                    "INSERT INTO tenant_inventory (tenant_id, ingredient_id, current_stock, minimum_stock, last_updated) VALUES ($1, $2, $3, 0, NOW())",
                                    tenant_id, modifier_ingredient["ingredient_id"], -total_deduction
                                )

                            await conn.execute(
                                """
                                INSERT INTO tenant_ingredient_movements (
                                    tenant_id, ingredient_id, movement_type,
                                    quantity_change, unit, previous_stock, new_stock,
                                    reference_table, reference_id, reason, created_by, created_at
                                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW())
                                """,
                                tenant_id,
                                modifier_ingredient["ingredient_id"],
                                "consumption",
                                -total_deduction,
                                modifier_ingredient["ingredient_unit"] or "und",
                                previous_stock,
                                new_stock,
                                "orders",
                                order_id,
                                f"Modificador {modifier['name']} - Orden #{order_row['order_number']}",
                                user_id
                            )

                    # Deduct inventory for product ingredients (direct + base recipes)
                    ingredients = await conn.fetch(
                        """
                        SELECT pr.ingredient_id, pr.quantity, pr.unit, i.name AS ingredient_name
                        FROM product_recipes pr
                        JOIN ingredients i ON pr.ingredient_id = i.id
                        WHERE pr.product_id = $1

                        UNION ALL

                        -- Issue #517: multiply by pbr.quantity
                        SELECT brt.ingredient_id, brt.base_quantity * pbr.quantity AS quantity, brt.unit, i.name AS ingredient_name
                        FROM product_base_recipes pbr
                        JOIN base_recipe_templates brt ON pbr.product_base_type_id = brt.product_base_type_id
                        JOIN ingredients i ON brt.ingredient_id = i.id
                        WHERE pbr.product_id = $1
                        """,
                        UUID(item["product_id"])
                    )

                    for ingredient in ingredients:
                        quantity_to_deduct = float(item["quantity"]) * float(ingredient["quantity"])
                        stock_row = await conn.fetchrow(
                            "SELECT current_stock FROM tenant_inventory WHERE ingredient_id = $1 AND tenant_id = $2",
                            ingredient["ingredient_id"],
                            tenant_id
                        )
                        previous_stock = float(stock_row["current_stock"]) if stock_row else 0.0
                        new_stock = previous_stock - quantity_to_deduct

                        if stock_row:
                            await conn.execute(
                                "UPDATE tenant_inventory SET current_stock = $1, last_updated = NOW() WHERE ingredient_id = $2 AND tenant_id = $3",
                                new_stock, ingredient["ingredient_id"], tenant_id
                            )
                        else:
                            await conn.execute(
                                "INSERT INTO tenant_inventory (tenant_id, ingredient_id, current_stock, minimum_stock, last_updated) VALUES ($1, $2, $3, 0, NOW())",
                                tenant_id, ingredient["ingredient_id"], -quantity_to_deduct
                            )

                        await conn.execute(
                            """
                            INSERT INTO tenant_ingredient_movements (
                                tenant_id, ingredient_id, movement_type,
                                quantity_change, unit, previous_stock, new_stock,
                                reference_table, reference_id, reason, created_by, created_at
                            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW())
                            """,
                            tenant_id,
                            ingredient["ingredient_id"],
                            "consumption",
                            -quantity_to_deduct,
                            ingredient["unit"],
                            previous_stock,
                            new_stock,
                            "orders",
                            order_id,
                            f"Venta de {item['quantity']}x {item.get('product_name', '')} - Orden #{order_row['order_number']}",
                            user_id
                        )

        # Award waros for completed manual order (fire-and-forget — never blocks)
        if customer_id:
            try:
                asyncio.create_task(
                    evaluate_and_award(order_row["id"], UUID(customer_id), tenant_id)
                )
            except Exception as _waros_err:
                logger.warning(f"Could not schedule waros evaluation: {_waros_err}")

        return {
            "success": True,
            "data": {
                "id": str(order_row["id"]),
                "order_number": int(order_row["order_number"]),
                "order_date": order_row["order_date"].isoformat(),
                "total_amount": float(total_amount),
                "status": "completed",
                "payment_method": payment_method,
            }
        }

    except (AuthenticationError, APIError):
        raise
    except Exception as e:
        logger.error(f"Error creating manual order: {str(e)}")
        raise APIError(f"Error creating manual order: {str(e)}", status_code=500)


async def get_products_sold(
    request: Request,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    category_id: Optional[str] = None,
    sort: str = "qty_desc",
) -> dict:
    """
    Get products sold aggregated by product, filtered by date range and category.
    Only includes orders with status = 'completed'.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            where_conditions = ["o.tenant_id = $1", "o.status = 'completed'"]
            params: List = [tenant_id]
            param_count = 1

            parsed_date_from = parse_date(date_from)
            parsed_date_to = parse_date(date_to)

            if parsed_date_from:
                param_count += 1
                where_conditions.append(
                    f"o.order_date >= (${param_count}::timestamp AT TIME ZONE 'America/Bogota')"
                )
                params.append(parsed_date_from)

            if parsed_date_to:
                param_count += 1
                where_conditions.append(
                    f"o.order_date < ((${param_count}::timestamp + interval '1 day') AT TIME ZONE 'America/Bogota')"
                )
                params.append(parsed_date_to)

            if category_id:
                param_count += 1
                where_conditions.append(f"p.category_id = ${param_count}::uuid")
                params.append(category_id)

            where_clause = " AND ".join(where_conditions)

            sort_map = {
                "qty_desc": "quantity_sold DESC",
                "revenue_desc": "total_revenue DESC",
                "name_asc": "product_name ASC",
            }
            order_by = sort_map.get(sort, "quantity_sold DESC")

            query = f"""
                SELECT
                    p.id::text AS product_id,
                    p.name AS product_name,
                    p.category_id::text AS category_id,
                    c.name AS category_name,
                    SUM(oi.quantity)::int AS quantity_sold,
                    SUM(oi.subtotal) AS total_revenue
                FROM order_items oi
                JOIN orders o ON o.id = oi.order_id
                JOIN product p ON p.id = oi.product_id
                LEFT JOIN categories c ON c.id = p.category_id
                WHERE {where_clause}
                GROUP BY p.id, p.name, p.category_id, c.name
                ORDER BY {order_by}
            """

            rows = await conn.fetch(query, *params)

            data = [
                {
                    "product_id": row["product_id"],
                    "product_name": row["product_name"],
                    "category_id": row["category_id"],
                    "category_name": row["category_name"],
                    "quantity_sold": row["quantity_sold"],
                    "total_revenue": float(row["total_revenue"]),
                }
                for row in rows
            ]

            total_qty = sum(r["quantity_sold"] for r in data)
            total_revenue = sum(r["total_revenue"] for r in data)

            return {
                "success": True,
                "data": data,
                "totals": {
                    "quantity_sold": total_qty,
                    "total_revenue": total_revenue,
                },
            }

    except (AuthenticationError, APIError):
        raise
    except Exception as e:
        logger.error(f"Error getting products sold: {str(e)}")
        raise APIError(f"Error getting products sold: {str(e)}", status_code=500)


async def send_invoice_email(
    request: Request,
    order_id: UUID,
    recipient_email: str,
) -> Dict[str, Any]:
    """
    Send the WARO-branded receipt email for an order's accepted invoice (warocol.com#603).

    Loads the order header + items + invoice + tenant business profile from DB
    (single connection, sequential reads), validates the invoice is accepted and
    has a PDF available in R2, then dispatches the existing `send_pos_receipt_email`
    helper which handles SES + template + PDF/XML attachment.

    Raises:
        HTTPException 404 — order not found for the session tenant
        HTTPException 422 — no invoice / invoice not accepted / PDF not available
        HTTPException 502 — SES rejected the send
    """
    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    async with get_db_connection() as conn:
        # 1. Order header (also enforces tenant ownership).
        order_row = await conn.fetchrow(
            """SELECT id, order_number, order_date, total_amount, payment_method,
                      discount_amount
               FROM orders
               WHERE id = $1 AND tenant_id = $2""",
            order_id, tenant_id,
        )
        if not order_row:
            raise HTTPException(status_code=404, detail="Order not found")

        # 2. Invoice header — must exist, be accepted, and have a PDF available.
        invoice_row = await conn.fetchrow(
            """SELECT prefix, invoice_number, cufe, status, r2_pdf_key
               FROM electronic_invoices
               WHERE order_id = $1 AND tenant_id = $2
               ORDER BY created_at DESC
               LIMIT 1""",
            order_id, tenant_id,
        )
        if not invoice_row:
            raise HTTPException(
                status_code=422,
                detail="Esta orden no tiene factura electrónica. Emitila antes de enviarla.",
            )
        if invoice_row['status'] != 'accepted':
            raise HTTPException(
                status_code=422,
                detail="La factura debe estar aceptada por DIAN para poder enviarla por correo.",
            )
        if not invoice_row['r2_pdf_key']:
            raise HTTPException(
                status_code=422,
                detail="El PDF de la factura aún no está disponible. Reintentá en unos segundos.",
            )

        # 3. Tax breakdown — same helpers get_order_by_id uses.
        tax_config = await _get_tenant_tax_config(conn, tenant_id)
        items_for_tax = await conn.fetch(
            """SELECT COALESCE(p.tax_category, 'standard') AS tax_category,
                      COALESCE(oi.subtotal, 0) AS subtotal
               FROM order_items oi
               JOIN product p ON p.id = oi.product_id
               WHERE oi.order_id = $1""",
            order_id,
        )
        std_tax, liq_tax, tax_label = _compute_tax_breakdown(items_for_tax, tax_config)

        # 4. Items in the shape `pos_receipt_template` expects.
        item_rows = await conn.fetch(
            """SELECT oi.id, oi.quantity, oi.subtotal,
                      p.name as product_name
               FROM order_items oi
               LEFT JOIN product p ON p.id = oi.product_id
               WHERE oi.order_id = $1
               ORDER BY oi.created_at""",
            order_id,
        )
        items = []
        for it in item_rows:
            modifiers_rows = await conn.fetch(
                """SELECT modifier_name, price_at_purchase
                   FROM order_item_modifiers WHERE order_item_id = $1""",
                it['id'],
            )
            items.append({
                'quantity': it['quantity'],
                'subtotal': float(it['subtotal']),
                'product': {'name': it['product_name'] or 'Producto'},
                'modifiers': [
                    {'name': m['modifier_name'], 'price': float(m['price_at_purchase'])}
                    for m in modifiers_rows
                ],
            })

        # 5. Business profile for the email header.
        profile_row = await conn.fetchrow(
            """SELECT COALESCE(p.display_name, t.name) AS display_name,
                      p.address, p.city, p.phone_number
               FROM tenants t
               LEFT JOIN tenant_public_profiles p ON p.tenant_id = t.id
               WHERE t.id = $1""",
            tenant_id,
        )

    discount_amount = float(order_row['discount_amount']) if order_row['discount_amount'] is not None else 0.0
    # Subtotal: only carried into the template when there's a discount (the
    # template falls back to total_amount when no discount). Matches the POS
    # cart caller convention at pos_cart_service.py:1577.
    subtotal_for_email = sum(float(it['subtotal']) for it in item_rows) if discount_amount > 0 else 0.0

    success = await send_pos_receipt_email(
        customer_email=recipient_email,
        order_number=int(order_row['order_number']),
        total_amount=float(order_row['total_amount']),
        payment_method=order_row['payment_method'] or '',
        items=items,
        order_date=order_row['order_date'],
        tenant_id=str(tenant_id),
        business_name=profile_row['display_name'] if profile_row else None,
        business_address=profile_row['address'] if profile_row else None,
        business_city=profile_row['city'] if profile_row else None,
        business_phone=profile_row['phone_number'] if profile_row else None,
        discount_amount=discount_amount,
        subtotal=subtotal_for_email,
        standard_tax=std_tax,
        liquor_tax=liq_tax,
        standard_tax_label=tax_label,
        invoice_prefix=invoice_row['prefix'],
        invoice_number=int(invoice_row['invoice_number']),
        invoice_cufe=invoice_row['cufe'],
    )

    if not success:
        raise HTTPException(
            status_code=502,
            detail="No se pudo enviar el correo. Intentá nuevamente en unos segundos.",
        )

    return {'success': True, 'sent_to': recipient_email}

"""
Online Orders Service
Authenticated, tenant-scoped listing of online orders for restaurant operators.
"""
from typing import Optional
from uuid import UUID
from fastapi import Request
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError, APIError
import logging

logger = logging.getLogger(__name__)


SORT_COLUMNS = {
    "order_number": "o.order_number",
    "order_date": "o.order_date",
    "scheduled_time": "o.scheduled_time",
    "total_amount": "o.total_amount",
    "status": "o.status",
}


async def get_online_orders_list(
    request: Request,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    sort_field: str = "order_date",
    sort_direction: str = "desc",
) -> dict:
    """
    Return paginated list of online orders scoped to the authenticated tenant.
    Excludes POS orders (online_cart_id IS NOT NULL filter).
    """
    try:
        session = require_valid_session(request)
        tenant_id = session.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            where_conditions = ["o.tenant_id = $1", "o.online_cart_id IS NOT NULL"]
            params = [tenant_id]
            param_count = 1

            if status:
                param_count += 1
                where_conditions.append(f"o.status = ${param_count}")
                params.append(status)

            where_clause = " AND ".join(where_conditions)

            # Total count
            count_row = await conn.fetchrow(
                f"SELECT COUNT(*) as total FROM orders o WHERE {where_clause}",
                *params
            )
            total_count = count_row['total']

            # Paginated SELECT
            param_count += 1
            limit_param = param_count
            param_count += 1
            offset_param = param_count

            sort_col = SORT_COLUMNS.get(sort_field, "o.order_date")
            sort_dir = "ASC" if sort_direction.lower() == "asc" else "DESC"

            rows = await conn.fetch(f"""
                SELECT
                    o.id,
                    o.order_number,
                    o.order_date,
                    o.scheduled_time,
                    o.total_amount,
                    o.status,
                    oc.order_type,
                    oc.delivery_instructions,
                    oc.verified_email
                FROM orders o
                JOIN online_carts oc ON oc.id = o.online_cart_id
                WHERE {where_clause}
                ORDER BY {sort_col} {sort_dir}
                LIMIT ${limit_param} OFFSET ${offset_param}
            """, *params, limit, offset)

            return {
                "success": True,
                "data": [
                    {
                        "id": str(r['id']),
                        "order_number": int(r['order_number']),
                        "order_date": r['order_date'].isoformat(),
                        "scheduled_time": r['scheduled_time'].isoformat() if r['scheduled_time'] else None,
                        "total_amount": float(r['total_amount']),
                        "status": r['status'],
                        "order_type": r['order_type'],
                        "delivery_instructions": r['delivery_instructions'],
                        "verified_email": r['verified_email'],
                    }
                    for r in rows
                ],
                "pagination": {
                    "total": total_count,
                    "limit": limit,
                    "offset": offset,
                    "has_more": (offset + limit) < total_count,
                }
            }

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting online orders list: {str(e)}")
        raise APIError(f"Error getting online orders list: {str(e)}", status_code=500)


async def get_online_order_by_id(
    request: Request,
    order_id: UUID,
) -> dict:
    """
    Return full detail of a single online order scoped to the authenticated tenant.
    Excludes POS orders (online_cart_id IS NOT NULL filter).
    Returns 404 if not found or not owned by tenant.
    """
    try:
        session = require_valid_session(request)
        tenant_id = session.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # 1. Order header
            row = await conn.fetchrow("""
                SELECT
                    o.id,
                    o.order_number,
                    o.order_date,
                    o.scheduled_time,
                    o.total_amount,
                    o.status,
                    o.payment_method,
                    oc.order_type,
                    oc.delivery_instructions,
                    oc.verified_email,
                    ap.address_line1,
                    ap.address_line2,
                    ap.city,
                    ap.delivery_notes,
                    ap.label AS address_label
                FROM orders o
                JOIN online_carts oc ON oc.id = o.online_cart_id
                LEFT JOIN addresses_profile ap ON ap.id = oc.delivery_address_id
                WHERE o.id = $1
                  AND o.tenant_id = $2
                  AND o.online_cart_id IS NOT NULL
            """, order_id, tenant_id)

            if not row:
                raise APIError("Order not found", status_code=404)

            # 2. Items
            item_rows = await conn.fetch("""
                SELECT
                    oi.id,
                    oi.quantity,
                    oi.price_at_purchase,
                    oi.subtotal,
                    pr.name AS product_name
                FROM order_items oi
                JOIN product pr ON pr.id = oi.product_id
                WHERE oi.order_id = $1
                ORDER BY oi.created_at
            """, order_id)

            # 3. Modifiers (single query for all items)
            modifiers_by_item: dict = {}
            if item_rows:
                item_ids = [r['id'] for r in item_rows]
                modifier_rows = await conn.fetch("""
                    SELECT order_item_id, modifier_name, price_at_purchase, quantity
                    FROM order_item_modifiers
                    WHERE order_item_id = ANY($1::uuid[])
                """, item_ids)

                for m in modifier_rows:
                    key = str(m['order_item_id'])
                    modifiers_by_item.setdefault(key, []).append({
                        "name": m['modifier_name'],
                        "price": float(m['price_at_purchase']),
                        "quantity": float(m['quantity']),
                    })

            # Build delivery_address only when present
            delivery_address = None
            if row['address_line1']:
                delivery_address = {
                    "address_line1": row['address_line1'],
                    "address_line2": row['address_line2'],
                    "city": row['city'],
                    "delivery_notes": row['delivery_notes'],
                    "label": row['address_label'],
                }

            return {
                "success": True,
                "data": {
                    "id": str(row['id']),
                    "order_number": int(row['order_number']),
                    "order_date": row['order_date'].isoformat(),
                    "scheduled_time": row['scheduled_time'].isoformat() if row['scheduled_time'] else None,
                    "total_amount": float(row['total_amount']),
                    "status": row['status'],
                    "payment_method": row['payment_method'],
                    "order_type": row['order_type'],
                    "delivery_instructions": row['delivery_instructions'],
                    "verified_email": row['verified_email'],
                    "delivery_address": delivery_address,
                    "items": [
                        {
                            "id": str(item['id']),
                            "product_name": item['product_name'],
                            "quantity": float(item['quantity']),
                            "unit_price": float(item['price_at_purchase']),
                            "subtotal": float(item['subtotal']),
                            "modifiers": modifiers_by_item.get(str(item['id']), []),
                        }
                        for item in item_rows
                    ],
                }
            }

    except AuthenticationError as e:
        raise e
    except APIError as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting online order by id {order_id}: {str(e)}")
        raise APIError(f"Error getting online order detail: {str(e)}", status_code=500)

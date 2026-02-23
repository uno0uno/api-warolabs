"""
Online Orders Service
Authenticated, tenant-scoped listing of online orders for restaurant operators.
"""
from typing import Optional
from fastapi import Request
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError, APIError
import logging

logger = logging.getLogger(__name__)


async def get_online_orders_list(
    request: Request,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
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
                ORDER BY o.order_date DESC
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

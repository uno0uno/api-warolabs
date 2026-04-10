"""
Cartera Service
Portfolio summary, aging report, and per-customer credit detail.

Issue: https://github.com/uno0uno/warocol.com/issues/308
"""
from typing import List, Dict, Any
from uuid import UUID
from fastapi import Request
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError, APIError
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared SQL fragment — classifies an order as overdue or current.
# Overdue when:
#   - credit_due_date is set and is in the past, OR
#   - credit_due_date is NULL and order was created more than 30 days ago.
# ---------------------------------------------------------------------------
_OVERDUE_EXPR = """
    (
        (o.credit_due_date IS NOT NULL AND o.credit_due_date < CURRENT_DATE)
        OR (o.credit_due_date IS NULL AND o.order_date < CURRENT_DATE - INTERVAL '30 days')
    )
"""

_DAYS_OUTSTANDING_EXPR = """
    CASE
        WHEN o.credit_due_date IS NOT NULL
            THEN GREATEST(0, (CURRENT_DATE - o.credit_due_date)::int)
        ELSE GREATEST(0, (DATE_PART('day', now() - o.order_date))::int - 30)
    END
"""


async def get_cartera_summary(request: Request) -> dict:
    """
    Global portfolio summary for the tenant.
    Returns total_outstanding, customer_count, overdue_count, overdue_amount.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection(use_transaction=False) as conn:
            row = await conn.fetchrow(
                f"""
                SELECT
                    COALESCE(SUM(o.total_amount - o.credit_paid_amount), 0)
                        AS total_outstanding,
                    COUNT(DISTINCT o.customer_id)
                        AS customer_count,
                    COUNT(DISTINCT CASE WHEN {_OVERDUE_EXPR} THEN o.customer_id END)
                        AS overdue_count,
                    COALESCE(SUM(
                        CASE WHEN {_OVERDUE_EXPR}
                             THEN o.total_amount - o.credit_paid_amount
                             ELSE 0
                        END
                    ), 0) AS overdue_amount
                FROM orders o
                WHERE o.tenant_id = $1
                  AND o.payment_status IN ('credit', 'partial')
                """,
                tenant_id,
            )

        return {
            "success": True,
            "data": {
                "total_outstanding": float(row["total_outstanding"]),
                "customer_count": int(row["customer_count"]),
                "overdue_count": int(row["overdue_count"]),
                "overdue_amount": float(row["overdue_amount"]),
            },
        }

    except (AuthenticationError, APIError):
        raise
    except Exception as exc:
        logger.error(f"Error in get_cartera_summary: {exc}")
        raise APIError(f"Error in get_cartera_summary: {exc}", status_code=500)


async def list_cartera_customers(
    request: Request,
    status: str = "all",
    sort: str = "balance_desc",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """
    Paginated list of customers with outstanding credit balance.
    Excludes customers whose ALL credit orders have been fully paid.

    status: all | overdue | current
    sort:   balance_desc | name_asc | days_desc
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        # Build status filter clause
        if status == "overdue":
            having_filter = f"HAVING BOOL_OR({_OVERDUE_EXPR})"
        elif status == "current":
            having_filter = f"HAVING NOT BOOL_OR({_OVERDUE_EXPR})"
        else:
            having_filter = ""

        # Build ORDER BY clause
        sort_map = {
            "balance_desc": "total_outstanding DESC",
            "name_asc": "customer_name ASC",
            "days_desc": "max_days_outstanding DESC",
        }
        order_clause = sort_map.get(sort, "total_outstanding DESC")

        async with get_db_connection(use_transaction=False) as conn:
            rows = await conn.fetch(
                f"""
                SELECT
                    o.customer_id,
                    p.name                                    AS customer_name,
                    p.phone_number                            AS customer_phone,
                    SUM(o.total_amount - o.credit_paid_amount) AS total_outstanding,
                    MAX({_DAYS_OUTSTANDING_EXPR})              AS max_days_outstanding,
                    BOOL_OR({_OVERDUE_EXPR})                  AS is_overdue,
                    COUNT(o.id)                               AS order_count,
                    COUNT(*) OVER ()                          AS total_count
                FROM orders o
                LEFT JOIN profile p ON o.customer_id = p.id
                WHERE o.tenant_id = $1
                  AND o.payment_status IN ('credit', 'partial')
                GROUP BY o.customer_id, p.name, p.phone_number
                {having_filter}
                ORDER BY {order_clause}
                LIMIT $2 OFFSET $3
                """,
                tenant_id,
                limit,
                offset,
            )

        total_count = int(rows[0]["total_count"]) if rows else 0

        return {
            "success": True,
            "data": [
                {
                    "customer_id": str(row["customer_id"]) if row["customer_id"] else None,
                    "name": row["customer_name"],
                    "phone": row["customer_phone"],
                    "total_outstanding": float(row["total_outstanding"]),
                    "oldest_order_days": int(row["max_days_outstanding"]),
                    "order_count": int(row["order_count"]),
                    "status": "overdue" if row["is_overdue"] else "current",
                }
                for row in rows
            ],
            "pagination": {
                "total": total_count,
                "limit": limit,
                "offset": offset,
                "has_more": (offset + limit) < total_count,
            },
        }

    except (AuthenticationError, APIError):
        raise
    except Exception as exc:
        logger.error(f"Error in list_cartera_customers: {exc}")
        raise APIError(f"Error in list_cartera_customers: {exc}", status_code=500)


async def get_customer_cartera(
    request: Request,
    customer_id: UUID,
) -> dict:
    """
    Credit detail for a single customer: summary + list of open credit orders
    with per-order payment history.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection(use_transaction=False) as conn:
            # Customer info
            customer_row = await conn.fetchrow(
                "SELECT id, name, phone_number, email FROM profile WHERE id = $1",
                customer_id,
            )
            if not customer_row:
                raise APIError("Cliente no encontrado", status_code=404)

            # Open credit orders for this customer under this tenant
            order_rows = await conn.fetch(
                f"""
                SELECT
                    o.id,
                    o.order_number,
                    o.order_date,
                    o.total_amount,
                    o.credit_paid_amount,
                    o.credit_due_date,
                    o.payment_status,
                    (o.total_amount - o.credit_paid_amount)  AS remaining,
                    {_DAYS_OUTSTANDING_EXPR}                 AS days_outstanding,
                    {_OVERDUE_EXPR}                          AS is_overdue
                FROM orders o
                WHERE o.tenant_id = $1
                  AND o.customer_id = $2
                  AND o.payment_status IN ('credit', 'partial')
                ORDER BY o.order_date DESC
                """,
                tenant_id,
                customer_id,
            )

            if not order_rows:
                # Customer exists but has no open credit — still return 200 with empty list
                return {
                    "success": True,
                    "data": {
                        "customer": {
                            "id": str(customer_row["id"]),
                            "name": customer_row["name"],
                            "phone": customer_row["phone_number"],
                            "email": customer_row["email"],
                        },
                        "summary": {
                            "total_outstanding": 0.0,
                            "order_count": 0,
                            "overdue_count": 0,
                            "overdue_amount": 0.0,
                        },
                        "orders": [],
                    },
                }

            # Fetch payment history for all open orders in a single query
            order_ids = [row["id"] for row in order_rows]
            payment_rows = await conn.fetch(
                """
                SELECT
                    cp.order_id,
                    cp.id,
                    cp.amount,
                    cp.payment_method,
                    cp.payment_date,
                    cp.notes,
                    cp.created_at
                FROM credit_payments cp
                WHERE cp.order_id = ANY($1::uuid[])
                  AND cp.tenant_id = $2
                ORDER BY cp.payment_date ASC
                """,
                order_ids,
                tenant_id,
            )

            # Index payments by order_id
            payments_by_order: Dict[str, List[Dict[str, Any]]] = {}
            for p in payment_rows:
                key = str(p["order_id"])
                payments_by_order.setdefault(key, [])
                payments_by_order[key].append(
                    {
                        "id": str(p["id"]),
                        "amount": float(p["amount"]),
                        "payment_method": p["payment_method"],
                        "payment_date": p["payment_date"].isoformat(),
                        "notes": p["notes"],
                        "created_at": p["created_at"].isoformat(),
                    }
                )

        # Build response
        total_outstanding = sum(float(r["remaining"]) for r in order_rows)
        overdue_rows = [r for r in order_rows if r["is_overdue"]]
        overdue_amount = sum(float(r["remaining"]) for r in overdue_rows)

        orders_out = []
        for r in order_rows:
            oid = str(r["id"])
            orders_out.append(
                {
                    "id": oid,
                    "order_number": int(r["order_number"]),
                    "date": r["order_date"].isoformat(),
                    "total_amount": float(r["total_amount"]),
                    "credit_paid_amount": float(r["credit_paid_amount"]),
                    "remaining": float(r["remaining"]),
                    "due_date": str(r["credit_due_date"]) if r["credit_due_date"] else None,
                    "days_outstanding": int(r["days_outstanding"]),
                    "payment_status": r["payment_status"],
                    "is_overdue": bool(r["is_overdue"]),
                    "payment_history": payments_by_order.get(oid, []),
                }
            )

        return {
            "success": True,
            "data": {
                "customer": {
                    "id": str(customer_row["id"]),
                    "name": customer_row["name"],
                    "phone": customer_row["phone_number"],
                    "email": customer_row["email"],
                },
                "summary": {
                    "total_outstanding": total_outstanding,
                    "order_count": len(order_rows),
                    "overdue_count": len(overdue_rows),
                    "overdue_amount": overdue_amount,
                },
                "orders": orders_out,
            },
        }

    except (AuthenticationError, APIError):
        raise
    except Exception as exc:
        logger.error(f"Error in get_customer_cartera: {exc}")
        raise APIError(f"Error in get_customer_cartera: {exc}", status_code=500)


async def get_cartera_aging(request: Request) -> dict:
    """
    Aging buckets computed at query time (no caching).
    Buckets: 0-30d, 31-60d, 61-90d, 90+d
    Each bucket: customer_count, total_amount
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection(use_transaction=False) as conn:
            rows = await conn.fetch(
                f"""
                WITH aged AS (
                    SELECT
                        o.customer_id,
                        (o.total_amount - o.credit_paid_amount) AS outstanding,
                        {_DAYS_OUTSTANDING_EXPR}                AS days_out
                    FROM orders o
                    WHERE o.tenant_id = $1
                      AND o.payment_status IN ('credit', 'partial')
                ),
                bucketed AS (
                    SELECT
                        customer_id,
                        outstanding,
                        CASE
                            WHEN days_out <= 30 THEN '0-30'
                            WHEN days_out <= 60 THEN '31-60'
                            WHEN days_out <= 90 THEN '61-90'
                            ELSE '90+'
                        END AS bucket
                    FROM aged
                )
                SELECT
                    bucket,
                    COUNT(DISTINCT customer_id)   AS customer_count,
                    COALESCE(SUM(outstanding), 0) AS total_amount
                FROM bucketed
                GROUP BY bucket
                ORDER BY
                    CASE bucket
                        WHEN '0-30'  THEN 1
                        WHEN '31-60' THEN 2
                        WHEN '61-90' THEN 3
                        ELSE 4
                    END
                """,
                tenant_id,
            )

        # Ensure all 4 buckets are present even if empty
        bucket_map = {
            "0-30":  {"label": "0–30 días",  "customer_count": 0, "total_amount": 0.0},
            "31-60": {"label": "31–60 días", "customer_count": 0, "total_amount": 0.0},
            "61-90": {"label": "61–90 días", "customer_count": 0, "total_amount": 0.0},
            "90+":   {"label": "90+ días",   "customer_count": 0, "total_amount": 0.0},
        }
        for row in rows:
            bucket_map[row["bucket"]]["customer_count"] = int(row["customer_count"])
            bucket_map[row["bucket"]]["total_amount"] = float(row["total_amount"])

        return {
            "success": True,
            "data": list(bucket_map.values()),
        }

    except (AuthenticationError, APIError):
        raise
    except Exception as exc:
        logger.error(f"Error in get_cartera_aging: {exc}")
        raise APIError(f"Error in get_cartera_aging: {exc}", status_code=500)

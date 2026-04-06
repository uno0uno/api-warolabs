"""
Tables Service
Business logic for table management and session lifecycle.

Issue: https://github.com/uno0uno/warocol.com/issues/298
"""
from typing import Optional, List
from uuid import UUID
from fastapi import Request
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError, APIError, NotFoundError
import logging

logger = logging.getLogger(__name__)


async def list_tables(request: Request) -> dict:
    """
    List all active tables for tenant with current status, session duration, and running total.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection(use_transaction=False) as conn:
            rows = await conn.fetch(
                """
                SELECT
                    t.id,
                    t.name,
                    t.capacity,
                    t.status,
                    t.is_active,
                    t.created_at,
                    ts.id            AS session_id,
                    ts.opened_at,
                    ts.opened_by_user_id,
                    EXTRACT(EPOCH FROM (now() - ts.opened_at)) / 60 AS session_duration_minutes,
                    COALESCE(
                        (SELECT SUM(o.total_amount)
                         FROM orders o
                         WHERE o.table_session_id = ts.id),
                        0
                    ) AS running_total
                FROM tables t
                LEFT JOIN table_sessions ts
                    ON ts.table_id = t.id
                    AND ts.tenant_id = $1
                    AND ts.closed_at IS NULL
                WHERE t.tenant_id = $1
                  AND t.is_active = true
                ORDER BY t.name
                """,
                tenant_id,
            )

        return {
            "success": True,
            "data": [_format_table_row(r) for r in rows],
        }

    except AuthenticationError:
        raise
    except Exception as e:
        logger.error(f"Error listing tables: {e}")
        raise APIError(f"Error listing tables: {e}", status_code=500)


async def create_table(
    request: Request,
    name: str,
    capacity: Optional[int],
) -> dict:
    """
    Create a new table for the tenant.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO tables (tenant_id, name, capacity)
                VALUES ($1, $2, $3)
                RETURNING id, name, capacity, status, is_active, created_at
                """,
                tenant_id,
                name,
                capacity,
            )

        logger.info(f"Table created: {row['id']} ({name}) for tenant {tenant_id}")
        return {"success": True, "data": _format_table_simple(row)}

    except AuthenticationError:
        raise
    except Exception as e:
        logger.error(f"Error creating table: {e}")
        raise APIError(f"Error creating table: {e}", status_code=500)


async def update_table(
    request: Request,
    table_id: UUID,
    name: Optional[str],
    capacity: Optional[int],
) -> dict:
    """
    Update a table's name and/or capacity (status is NOT editable here).
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            existing = await conn.fetchrow(
                "SELECT id FROM tables WHERE id = $1 AND tenant_id = $2 AND is_active = true",
                table_id,
                tenant_id,
            )
            if not existing:
                raise NotFoundError("Table not found")

            row = await conn.fetchrow(
                """
                UPDATE tables
                SET
                    name     = COALESCE($3, name),
                    capacity = COALESCE($4, capacity)
                WHERE id = $1 AND tenant_id = $2
                RETURNING id, name, capacity, status, is_active, created_at
                """,
                table_id,
                tenant_id,
                name,
                capacity,
            )

        return {"success": True, "data": _format_table_simple(row)}

    except (AuthenticationError, NotFoundError):
        raise
    except Exception as e:
        logger.error(f"Error updating table {table_id}: {e}")
        raise APIError(f"Error updating table: {e}", status_code=500)


async def soft_delete_table(request: Request, table_id: UUID) -> dict:
    """
    Soft-delete a table (is_active = false).
    Returns 409 if the table has an open session.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            existing = await conn.fetchrow(
                "SELECT id FROM tables WHERE id = $1 AND tenant_id = $2 AND is_active = true",
                table_id,
                tenant_id,
            )
            if not existing:
                raise NotFoundError("Table not found")

            open_session = await conn.fetchrow(
                "SELECT id FROM table_sessions WHERE table_id = $1 AND tenant_id = $2 AND closed_at IS NULL",
                table_id,
                tenant_id,
            )
            if open_session:
                raise APIError("Cannot delete a table with an open session", status_code=409)

            await conn.execute(
                "UPDATE tables SET is_active = false WHERE id = $1 AND tenant_id = $2",
                table_id,
                tenant_id,
            )

        logger.info(f"Table soft-deleted: {table_id}")
        return {"success": True, "message": "Table deactivated"}

    except (AuthenticationError, NotFoundError, APIError):
        raise
    except Exception as e:
        logger.error(f"Error deleting table {table_id}: {e}")
        raise APIError(f"Error deleting table: {e}", status_code=500)


async def open_session(request: Request, table_id: UUID) -> dict:
    """
    Open a new session for a table.
    Returns 409 if a session is already open.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                # Lock the table row to prevent concurrent opens
                table_row = await conn.fetchrow(
                    "SELECT id, status FROM tables WHERE id = $1 AND tenant_id = $2 AND is_active = true FOR UPDATE",
                    table_id,
                    tenant_id,
                )
                if not table_row:
                    raise NotFoundError("Table not found")

                # Check for existing open session
                open_session_row = await conn.fetchrow(
                    "SELECT id FROM table_sessions WHERE table_id = $1 AND tenant_id = $2 AND closed_at IS NULL",
                    table_id,
                    tenant_id,
                )
                if open_session_row:
                    raise APIError("Table already has an open session", status_code=409)

                # Create session
                session_row = await conn.fetchrow(
                    """
                    INSERT INTO table_sessions (table_id, tenant_id, opened_by_user_id)
                    VALUES ($1, $2, $3)
                    RETURNING id, opened_at
                    """,
                    table_id,
                    tenant_id,
                    user_id,
                )

                # Update table status
                await conn.execute(
                    "UPDATE tables SET status = 'open' WHERE id = $1 AND tenant_id = $2",
                    table_id,
                    tenant_id,
                )

        logger.info(f"Session opened: {session_row['id']} for table {table_id}")
        return {
            "success": True,
            "data": {
                "session_id": str(session_row["id"]),
                "table_id": str(table_id),
                "opened_at": session_row["opened_at"].isoformat(),
            },
        }

    except (AuthenticationError, NotFoundError, APIError):
        raise
    except Exception as e:
        logger.error(f"Error opening session for table {table_id}: {e}")
        raise APIError(f"Error opening session: {e}", status_code=500)


async def close_session(request: Request, table_id: UUID) -> dict:
    """
    Close the active session for a table.
    Pending orders on this session remain pending — frontend routes to payment.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                table_row = await conn.fetchrow(
                    "SELECT id FROM tables WHERE id = $1 AND tenant_id = $2 AND is_active = true FOR UPDATE",
                    table_id,
                    tenant_id,
                )
                if not table_row:
                    raise NotFoundError("Table not found")

                session_row = await conn.fetchrow(
                    "SELECT id FROM table_sessions WHERE table_id = $1 AND tenant_id = $2 AND closed_at IS NULL",
                    table_id,
                    tenant_id,
                )
                if not session_row:
                    raise NotFoundError("No open session found for this table")

                # Count pending orders (informational — not changed)
                pending_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM orders WHERE table_session_id = $1 AND status = 'pending'",
                    session_row["id"],
                )

                # Close session
                await conn.execute(
                    "UPDATE table_sessions SET closed_at = now() WHERE id = $1",
                    session_row["id"],
                )

                # Reset table status to free
                await conn.execute(
                    "UPDATE tables SET status = 'free' WHERE id = $1 AND tenant_id = $2",
                    table_id,
                    tenant_id,
                )

        logger.info(f"Session closed: {session_row['id']} for table {table_id}")
        return {
            "success": True,
            "data": {
                "session_id": str(session_row["id"]),
                "table_id": str(table_id),
                "pending_orders": int(pending_count),
            },
        }

    except (AuthenticationError, NotFoundError, APIError):
        raise
    except Exception as e:
        logger.error(f"Error closing session for table {table_id}: {e}")
        raise APIError(f"Error closing session: {e}", status_code=500)


async def get_current_session(request: Request, table_id: UUID) -> dict:
    """
    Get the open session for a table with all linked orders and running total.
    Returns 404 if no open session exists.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection(use_transaction=False) as conn:
            table_row = await conn.fetchrow(
                "SELECT id, name, status FROM tables WHERE id = $1 AND tenant_id = $2 AND is_active = true",
                table_id,
                tenant_id,
            )
            if not table_row:
                raise NotFoundError("Table not found")

            session_row = await conn.fetchrow(
                """
                SELECT
                    ts.id,
                    ts.opened_at,
                    ts.opened_by_user_id,
                    EXTRACT(EPOCH FROM (now() - ts.opened_at)) / 60 AS duration_minutes,
                    COALESCE(
                        (SELECT SUM(o.total_amount)
                         FROM orders o
                         WHERE o.table_session_id = ts.id),
                        0
                    ) AS running_total,
                    COALESCE(
                        (SELECT COUNT(*)
                         FROM orders o
                         WHERE o.table_session_id = ts.id),
                        0
                    ) AS order_count
                FROM table_sessions ts
                WHERE ts.table_id = $1
                  AND ts.tenant_id = $2
                  AND ts.closed_at IS NULL
                """,
                table_id,
                tenant_id,
            )
            if not session_row:
                raise NotFoundError("No open session for this table")

            # Fetch orders linked to this session
            orders = await conn.fetch(
                """
                SELECT id, total_amount, status, order_date, payment_method, order_number
                FROM orders
                WHERE table_session_id = $1
                ORDER BY order_date ASC
                """,
                session_row["id"],
            )

        return {
            "success": True,
            "data": {
                "table": {
                    "id": str(table_id),
                    "name": table_row["name"],
                    "status": table_row["status"],
                },
                "session": {
                    "id": str(session_row["id"]),
                    "opened_at": session_row["opened_at"].isoformat(),
                    "duration_minutes": round(float(session_row["duration_minutes"]), 1),
                    "running_total": float(session_row["running_total"]),
                    "order_count": int(session_row["order_count"]),
                },
                "orders": [
                    {
                        "id": str(o["id"]),
                        "order_number": o["order_number"],
                        "total_amount": float(o["total_amount"]),
                        "status": o["status"],
                        "payment_method": o["payment_method"],
                        "order_date": o["order_date"].isoformat(),
                    }
                    for o in orders
                ],
            },
        }

    except (AuthenticationError, NotFoundError):
        raise
    except Exception as e:
        logger.error(f"Error fetching current session for table {table_id}: {e}")
        raise APIError(f"Error fetching session: {e}", status_code=500)


async def request_bill(request: Request, table_id: UUID) -> dict:
    """
    Mark a table as bill_requested (status: open → bill_requested).
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                table_row = await conn.fetchrow(
                    "SELECT id, status FROM tables WHERE id = $1 AND tenant_id = $2 AND is_active = true FOR UPDATE",
                    table_id,
                    tenant_id,
                )
                if not table_row:
                    raise NotFoundError("Table not found")

                if table_row["status"] != "open":
                    raise APIError(
                        f"Table must be open to request bill (current status: {table_row['status']})",
                        status_code=409,
                    )

                await conn.execute(
                    "UPDATE tables SET status = 'bill_requested' WHERE id = $1 AND tenant_id = $2",
                    table_id,
                    tenant_id,
                )

        return {"success": True, "data": {"table_id": str(table_id), "status": "bill_requested"}}

    except (AuthenticationError, NotFoundError, APIError):
        raise
    except Exception as e:
        logger.error(f"Error requesting bill for table {table_id}: {e}")
        raise APIError(f"Error requesting bill: {e}", status_code=500)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _format_table_row(row: dict) -> dict:
    result = {
        "id": str(row["id"]),
        "name": row["name"],
        "capacity": row["capacity"],
        "status": row["status"],
        "is_active": row["is_active"],
        "created_at": row["created_at"].isoformat(),
        "session": None,
    }
    if row["session_id"]:
        result["session"] = {
            "id": str(row["session_id"]),
            "opened_at": row["opened_at"].isoformat(),
            "duration_minutes": round(float(row["session_duration_minutes"]), 1),
            "running_total": float(row["running_total"]),
        }
    return result


def _format_table_simple(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "capacity": row["capacity"],
        "status": row["status"],
        "is_active": row["is_active"],
        "created_at": row["created_at"].isoformat(),
    }

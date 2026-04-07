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


async def close_session(request: Request, table_id: UUID, payment_method: Optional[str] = None, customer_id: Optional[str] = None) -> dict:
    """
    Close the active session for a table.
    If payment_method is provided, marks all pending orders as completed with that payment method.
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

                # Mark pending orders as completed if payment_method provided
                if payment_method:
                    completed_count = await conn.fetchval(
                        "SELECT COUNT(*) FROM orders WHERE table_session_id = $1 AND status = 'pending'",
                        session_row["id"],
                    )
                    await conn.execute(
                        """
                        UPDATE orders
                        SET status = 'completed', payment_method = $2, customer_id = COALESCE($3::uuid, customer_id)
                        WHERE table_session_id = $1 AND status = 'pending'
                        """,
                        session_row["id"],
                        payment_method,
                        customer_id,
                    )
                    logger.info(
                        f"[close_session] Marked {completed_count} orders as completed "
                        f"(payment_method={payment_method}) for session {session_row['id']}"
                    )
                else:
                    completed_count = 0

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
                "completed_orders": int(completed_count or 0),
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

            # Fetch individual order items for this session (with IDs for edit/delete)
            tab_items = await conn.fetch(
                """
                SELECT
                    oi.id AS order_item_id,
                    oi.quantity,
                    oi.price_at_purchase,
                    oi.subtotal,
                    p.name AS product_name
                FROM order_items oi
                JOIN orders o ON o.id = oi.order_id
                JOIN product p ON p.id = oi.product_id
                WHERE o.table_session_id = $1
                ORDER BY oi.id ASC
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
                "tab_items": [
                    {
                        "order_item_id": str(r["order_item_id"]),
                        "product_name": r["product_name"],
                        "quantity": int(r["quantity"]),
                        "unit_price": float(r["subtotal"]) / int(r["quantity"]) if int(r["quantity"]) > 0 else float(r["price_at_purchase"]),
                        "subtotal": float(r["subtotal"]),
                    }
                    for r in tab_items
                ],
            },
        }

    except (AuthenticationError, NotFoundError):
        raise
    except Exception as e:
        logger.error(f"Error fetching current session for table {table_id}: {e}")
        raise APIError(f"Error fetching session: {e}", status_code=500)


async def remove_tab_item(request: Request, table_id: UUID, order_item_id: UUID) -> dict:
    """Remove an order item from the running tab and update the order total."""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection(use_transaction=True) as conn:
            row = await conn.fetchrow(
                """
                SELECT oi.id, oi.subtotal, o.id AS order_id, o.total_amount
                FROM order_items oi
                JOIN orders o ON o.id = oi.order_id
                JOIN table_sessions ts ON ts.id = o.table_session_id
                WHERE oi.id = $1
                  AND ts.table_id = $2
                  AND ts.tenant_id = $3
                  AND ts.closed_at IS NULL
                """,
                order_item_id, table_id, tenant_id,
            )
            if not row:
                raise NotFoundError("Order item not found or session already closed")

            await conn.execute("DELETE FROM order_items WHERE id = $1", order_item_id)
            new_total = max(0.0, float(row["total_amount"]) - float(row["subtotal"]))
            await conn.execute(
                "UPDATE orders SET total_amount = $1 WHERE id = $2",
                new_total, row["order_id"],
            )

        return {"success": True, "data": {"removed": str(order_item_id)}}
    except (AuthenticationError, NotFoundError):
        raise
    except Exception as e:
        logger.error(f"Error removing tab item {order_item_id}: {e}")
        raise APIError(f"Error removing item: {e}", status_code=500)


async def update_tab_item_quantity(
    request: Request, table_id: UUID, order_item_id: UUID, quantity: int
) -> dict:
    """Update quantity of an order item in the running tab."""
    if quantity < 1:
        raise APIError("Quantity must be at least 1", status_code=400)
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection(use_transaction=True) as conn:
            row = await conn.fetchrow(
                """
                SELECT oi.id, oi.price_at_purchase, oi.subtotal, o.id AS order_id, o.total_amount
                FROM order_items oi
                JOIN orders o ON o.id = oi.order_id
                JOIN table_sessions ts ON ts.id = o.table_session_id
                WHERE oi.id = $1
                  AND ts.table_id = $2
                  AND ts.tenant_id = $3
                  AND ts.closed_at IS NULL
                """,
                order_item_id, table_id, tenant_id,
            )
            if not row:
                raise NotFoundError("Order item not found or session already closed")

            modifier_sum = await conn.fetchval(
                "SELECT COALESCE(SUM(price_at_purchase), 0) FROM order_item_modifiers WHERE order_item_id = $1",
                order_item_id,
            )
            effective_unit_price = float(row["price_at_purchase"]) + float(modifier_sum)
            new_subtotal = effective_unit_price * quantity
            old_subtotal = float(row["subtotal"])
            await conn.execute(
                "UPDATE order_items SET quantity = $1, subtotal = $2 WHERE id = $3",
                quantity, new_subtotal, order_item_id,
            )
            new_total = max(0.0, float(row["total_amount"]) - old_subtotal + new_subtotal)
            await conn.execute(
                "UPDATE orders SET total_amount = $1 WHERE id = $2",
                new_total, row["order_id"],
            )

        return {"success": True, "data": {"order_item_id": str(order_item_id), "quantity": quantity, "subtotal": new_subtotal}}
    except (AuthenticationError, NotFoundError):
        raise
    except Exception as e:
        logger.error(f"Error updating tab item {order_item_id}: {e}")
        raise APIError(f"Error updating item: {e}", status_code=500)


async def add_tab_items(
    request: Request,
    table_id: UUID,
    items: List[dict],
) -> dict:
    """
    Add items to the running tab for a table session.
    Creates a pending order linked to the table_session_id — no payment method required.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")
        if not items:
            raise APIError("No items provided", status_code=400)

        async with get_db_connection() as conn:
            async with conn.transaction():
                # Verify the table has an open session owned by this tenant
                session_row = await conn.fetchrow(
                    """
                    SELECT ts.id AS session_id
                    FROM table_sessions ts
                    JOIN tables t ON t.id = ts.table_id
                    WHERE ts.table_id = $1
                      AND ts.tenant_id = $2
                      AND ts.closed_at IS NULL
                      AND t.is_active = true
                    """,
                    table_id,
                    tenant_id,
                )
                if not session_row:
                    raise NotFoundError("No open session found for this table")

                session_id = session_row["session_id"]

                # Compute total including modifier prices (no DB price lookup — POS already verified)
                for item in items:
                    mod_sum = sum(float(m.get("price", 0)) for m in (item.get("modifiers") or []))
                    logger.info(
                        f"[add_tab_items] item product_id={item['product_id']} "
                        f"qty={item['quantity']} unit_price={item['unit_price']} "
                        f"modifier_sum={mod_sum} "
                        f"line_total={item['quantity'] * (item['unit_price'] + mod_sum)}"
                    )
                total_amount = sum(
                    item["quantity"] * (
                        item["unit_price"]
                        + sum(float(m.get("price", 0)) for m in (item.get("modifiers") or []))
                    )
                    for item in items
                )
                logger.info(f"[add_tab_items] total_amount={total_amount} items_count={len(items)}")

                # Create pending order linked to the session
                order_row = await conn.fetchrow(
                    """
                    INSERT INTO orders (
                        user_id, tenant_id, table_session_id,
                        order_date, total_amount, status
                    )
                    VALUES ($1, $2, $3, NOW(), $4, 'pending')
                    RETURNING id, order_number
                    """,
                    user_id,
                    tenant_id,
                    session_id,
                    total_amount,
                )
                order_id = order_row["id"]

                # Insert order_items
                for item in items:
                    modifier_unit_total = sum(float(m.get("price", 0)) for m in (item.get("modifiers") or []))
                    subtotal = item["quantity"] * (item["unit_price"] + modifier_unit_total)
                    order_item_row = await conn.fetchrow(
                        """
                        INSERT INTO order_items (
                            order_id, product_id, quantity,
                            price_at_purchase, subtotal
                        )
                        VALUES ($1, $2, $3, $4, $5)
                        RETURNING id
                        """,
                        order_id,
                        item["product_id"],
                        item["quantity"],
                        item["unit_price"],
                        subtotal,
                    )
                    order_item_id = order_item_row["id"]

                    # Insert modifiers if any
                    for mod in item.get("modifiers") or []:
                        await conn.execute(
                            """
                            INSERT INTO order_item_modifiers (
                                order_item_id, modifier_id, modifier_name,
                                price_at_purchase, quantity
                            )
                            VALUES ($1, $2, $3, $4, $5)
                            """,
                            order_item_id,
                            mod.get("id"),
                            mod.get("name"),
                            mod.get("price", 0),
                            1,
                        )

        logger.info(f"Tab items added: order {order_id} for table {table_id}")
        return {
            "success": True,
            "data": {
                "order_id": str(order_id),
                "order_number": order_row["order_number"],
                "items_count": len(items),
                "total_amount": total_amount,
            },
        }

    except (AuthenticationError, NotFoundError, APIError):
        raise
    except Exception as e:
        logger.error(f"Error adding tab items for table {table_id}: {e}")
        raise APIError(f"Error adding tab items: {e}", status_code=500)


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


async def clear_tab(request: Request, table_id: UUID) -> dict:
    """
    Delete all pending orders (and their items) linked to the active session of a table.
    Does NOT close the session — the table stays open and ready for new orders.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                session_row = await conn.fetchrow(
                    "SELECT id FROM table_sessions WHERE table_id = $1 AND tenant_id = $2 AND closed_at IS NULL",
                    table_id,
                    tenant_id,
                )
                if not session_row:
                    raise NotFoundError("No open session found for this table")

                # Delete order_items first (FK: order_items.order_id → orders.id, no cascade)
                await conn.execute(
                    """
                    DELETE FROM order_items
                    WHERE order_id IN (
                        SELECT id FROM orders
                        WHERE table_session_id = $1 AND status = 'pending'
                    )
                    """,
                    session_row["id"],
                )

                deleted = await conn.fetchval(
                    """
                    WITH deleted AS (
                        DELETE FROM orders
                        WHERE table_session_id = $1 AND status = 'pending'
                        RETURNING id
                    )
                    SELECT COUNT(*) FROM deleted
                    """,
                    session_row["id"],
                )

        logger.info(f"[clear_tab] Deleted {deleted} pending orders for session {session_row['id']}")
        return {"success": True, "data": {"deleted_orders": int(deleted)}}

    except (AuthenticationError, NotFoundError, APIError):
        raise
    except Exception as e:
        logger.error(f"Error clearing tab for table {table_id}: {e}")
        raise APIError(f"Error clearing tab: {e}", status_code=500)


def _format_table_simple(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "capacity": row["capacity"],
        "status": row["status"],
        "is_active": row["is_active"],
        "created_at": row["created_at"].isoformat(),
    }

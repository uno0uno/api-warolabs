"""
Tables Service
Business logic for table management and session lifecycle.

Issue: https://github.com/uno0uno/warocol.com/issues/298
"""
from typing import Optional, List
from uuid import UUID
from datetime import date
from fastapi import Request
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError, APIError, NotFoundError
import logging

logger = logging.getLogger(__name__)


def _distribute_discount(items: List[dict], discount_amount: float) -> List[dict]:
    """Distribute discount proportionally across items by subtotal. Remainder goes to largest item."""
    total_subtotal = sum(item['subtotal'] for item in items)
    if total_subtotal <= 0 or discount_amount <= 0:
        for item in items:
            item['discount_allocated'] = 0
            item['net_total'] = float(item['subtotal'])
        return items
    allocated_total = 0.0
    for item in items:
        share = float(item['subtotal']) / total_subtotal
        item['discount_allocated'] = round(discount_amount * share)
        item['net_total'] = float(item['subtotal']) - item['discount_allocated']
        allocated_total += item['discount_allocated']
    remainder = round(discount_amount) - round(allocated_total)
    if remainder != 0:
        largest = max(items, key=lambda x: x['subtotal'])
        largest['discount_allocated'] += remainder
        largest['net_total'] -= remainder
    return items


async def _ensure_bar_table(conn, tenant_id) -> None:
    """
    Ensure a permanent bar table and its open session exist for the tenant.
    Called from list_tables — idempotent no-op if bar already exists.
    """
    bar_row = await conn.fetchrow(
        "SELECT id, status FROM tables WHERE tenant_id = $1 AND is_bar IS TRUE AND is_active = true LIMIT 1",
        tenant_id,
    )
    if not bar_row:
        bar_row = await conn.fetchrow(
            """
            INSERT INTO tables (tenant_id, name, capacity, status, is_bar)
            VALUES ($1, 'Barra', NULL, 'open', TRUE)
            RETURNING id, status
            """,
            tenant_id,
        )

    bar_table_id = bar_row["id"]

    # Ensure an open session exists for the bar
    open_session = await conn.fetchrow(
        "SELECT id FROM table_sessions WHERE table_id = $1 AND tenant_id = $2 AND closed_at IS NULL LIMIT 1",
        bar_table_id,
        tenant_id,
    )
    if not open_session:
        await conn.execute(
            """
            INSERT INTO table_sessions (table_id, tenant_id, opened_by_user_id)
            VALUES ($1, $2, NULL)
            """,
            bar_table_id,
            tenant_id,
        )

    # Ensure bar table status is 'open'
    if bar_row["status"] != "open":
        await conn.execute(
            "UPDATE tables SET status = 'open' WHERE id = $1 AND tenant_id = $2",
            bar_table_id,
            tenant_id,
        )


async def list_tables(request: Request) -> dict:
    """
    List all active tables for tenant with current status, session duration, and running total.
    Auto-creates the permanent Barra table + session if it doesn't exist yet.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                await _ensure_bar_table(conn, tenant_id)

            rows = await conn.fetch(
                """
                SELECT
                    t.id,
                    t.name,
                    t.capacity,
                    t.status,
                    t.is_active,
                    t.is_bar,
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
                    ) AS running_total,
                    (SELECT ts2.closed_at
                     FROM table_sessions ts2
                     WHERE ts2.table_id = t.id
                       AND ts2.tenant_id = $1
                       AND ts2.closed_at IS NOT NULL
                       AND ts2.is_discarded = FALSE
                     ORDER BY ts2.closed_at DESC
                     LIMIT 1) AS last_closed_at,
                    (SELECT ts3.id
                     FROM table_sessions ts3
                     WHERE ts3.table_id = t.id
                       AND ts3.tenant_id = $1
                       AND ts3.closed_at IS NOT NULL
                       AND ts3.is_discarded = FALSE
                     ORDER BY ts3.closed_at DESC
                     LIMIT 1) AS last_closed_session_id
                FROM tables t
                LEFT JOIN table_sessions ts
                    ON ts.table_id = t.id
                    AND ts.tenant_id = $1
                    AND ts.closed_at IS NULL
                WHERE t.tenant_id = $1
                  AND t.is_active = true
                ORDER BY t.is_bar DESC, t.name
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
                "SELECT id, is_bar FROM tables WHERE id = $1 AND tenant_id = $2 AND is_active = true",
                table_id,
                tenant_id,
            )
            if not existing:
                raise NotFoundError("Table not found")

            if existing["is_bar"]:
                raise APIError("La Barra no puede ser desactivada", status_code=409)

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


async def close_session(request: Request, table_id: UUID, payment_method: Optional[str] = None, customer_id: Optional[str] = None, credit_due_date: Optional[date] = None, payment_method_id: Optional[UUID] = None, discount_type: Optional[str] = None, discount_value: Optional[float] = None) -> dict:
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
                    "SELECT id, is_bar FROM tables WHERE id = $1 AND tenant_id = $2 AND is_active = true FOR UPDATE",
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

                is_bar_table = bool(table_row["is_bar"])

                # Mark pending orders as completed if payment_method provided
                if payment_method:
                    # Backend guard: credit requires an identified (non-anonymous) customer
                    if payment_method == 'credit' and customer_id:
                        cust_row = await conn.fetchrow(
                            "SELECT phone_number FROM profile WHERE id = $1::uuid",
                            customer_id
                        )
                        if cust_row and cust_row['phone_number'] == '0000000000':
                            raise APIError(
                                "El pago a crédito requiere un cliente identificado (no anónimo)",
                                status_code=400
                            )

                    payment_status = 'credit' if payment_method == 'credit' else 'paid'

                    # Compute discount if provided
                    _discount_amount = None
                    if discount_type and discount_value is not None and discount_value > 0:
                        # Sum all pending order totals for this session
                        session_total_row = await conn.fetchrow(
                            "SELECT COALESCE(SUM(total_amount), 0) AS total FROM orders WHERE table_session_id = $1 AND status = 'pending'",
                            session_row["id"],
                        )
                        session_total = float(session_total_row["total"])
                        if session_total > 0:
                            if discount_type == 'percent':
                                _discount_amount = round(session_total * discount_value / 100)
                            else:
                                _discount_amount = min(round(discount_value), round(session_total))

                    # If discount applies, distribute proportionally across all pending order_items
                    if _discount_amount:
                        item_rows = await conn.fetch(
                            """
                            SELECT oi.id, oi.subtotal
                            FROM order_items oi
                            JOIN orders o ON o.id = oi.order_id
                            WHERE o.table_session_id = $1 AND o.status = 'pending'
                            """,
                            session_row["id"],
                        )
                        items_for_dist = [
                            {"id": str(row["id"]), "subtotal": float(row["subtotal"])}
                            for row in item_rows
                        ]
                        items_for_dist = _distribute_discount(items_for_dist, float(_discount_amount))
                        for item in items_for_dist:
                            await conn.execute(
                                "UPDATE order_items SET discount_allocated = $2, net_total = $3 WHERE id = $1::uuid",
                                item["id"],
                                item["discount_allocated"],
                                item["net_total"],
                            )

                    completed_count = await conn.fetchval(
                        "SELECT COUNT(*) FROM orders WHERE table_session_id = $1 AND status = 'pending'",
                        session_row["id"],
                    )

                    if _discount_amount:
                        await conn.execute(
                            """
                            UPDATE orders
                            SET status = 'completed',
                                payment_method = $2,
                                payment_status = $3,
                                credit_due_date = $4,
                                customer_id = COALESCE($5::uuid, customer_id),
                                payment_method_id = $6,
                                discount_type = $7,
                                discount_value = $8,
                                discount_amount = $9,
                                total_amount = total_amount - $9
                            WHERE table_session_id = $1 AND status = 'pending'
                            """,
                            session_row["id"],
                            payment_method,
                            payment_status,
                            credit_due_date,
                            customer_id,
                            payment_method_id,
                            discount_type,
                            discount_value,
                            _discount_amount,
                        )
                    else:
                        await conn.execute(
                            """
                            UPDATE orders
                            SET status = 'completed',
                                payment_method = $2,
                                payment_status = $3,
                                credit_due_date = $4,
                                customer_id = COALESCE($5::uuid, customer_id),
                                payment_method_id = $6
                            WHERE table_session_id = $1 AND status = 'pending'
                            """,
                            session_row["id"],
                            payment_method,
                            payment_status,
                            credit_due_date,
                            customer_id,
                            payment_method_id,
                        )
                    logger.info(
                        f"[close_session] Marked {completed_count} orders as completed "
                        f"(payment_method={payment_method}, payment_status={payment_status}, "
                        f"discount_amount={_discount_amount}) for session {session_row['id']}"
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

                if is_bar_table:
                    # Bar table: immediately reopen a new session so the bar is always active.
                    # Do NOT reset status to 'free' — bar stays 'open'.
                    await conn.execute(
                        """
                        INSERT INTO table_sessions (table_id, tenant_id, opened_by_user_id)
                        VALUES ($1, $2, NULL)
                        """,
                        table_id,
                        tenant_id,
                    )
                    logger.info(f"Bar session rotated: {session_row['id']} for table {table_id}")
                else:
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
                batch_amount = sum(
                    item["quantity"] * (
                        item["unit_price"]
                        + sum(float(m.get("price", 0)) for m in (item.get("modifiers") or []))
                    )
                    for item in items
                )
                logger.info(f"[add_tab_items] batch_amount={batch_amount} items_count={len(items)}")

                # Reuse the existing pending order for this session, or create one if none exists
                existing_order = await conn.fetchrow(
                    """
                    SELECT id FROM orders
                    WHERE table_session_id = $1 AND status = 'pending'
                    ORDER BY order_date ASC
                    LIMIT 1
                    """,
                    session_id,
                )
                order_number: int
                order_total: float
                if existing_order:
                    order_id = existing_order["id"]
                    updated = await conn.fetchrow(
                        """
                        UPDATE orders
                        SET total_amount = total_amount + $1
                        WHERE id = $2
                        RETURNING order_number, total_amount
                        """,
                        batch_amount,
                        order_id,
                    )
                    order_number = updated["order_number"]
                    order_total = float(updated["total_amount"])
                    logger.info(f"[add_tab_items] reusing existing order {order_id}, adding {batch_amount}")
                else:
                    order_row = await conn.fetchrow(
                        """
                        INSERT INTO orders (
                            user_id, tenant_id, table_session_id,
                            order_date, total_amount, status
                        )
                        VALUES ($1, $2, $3, NOW(), $4, 'pending')
                        RETURNING id, order_number, total_amount
                        """,
                        user_id,
                        tenant_id,
                        session_id,
                        batch_amount,
                    )
                    order_id = order_row["id"]
                    order_number = order_row["order_number"]
                    order_total = float(order_row["total_amount"])
                    logger.info(f"[add_tab_items] created new order {order_id}")

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
                "order_number": order_number,
                "items_count": len(items),
                "total_amount": order_total,
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


async def discard_table_session(request: Request, table_id: UUID) -> dict:
    """
    Discard the active session for a table.
    Hard-deletes all pending orders/items, soft-closes the session (is_discarded=TRUE),
    and resets the table status to 'free'.
    Returns 404 if no open session. Returns 409 for bar table or completed orders.

    Issue: https://github.com/uno0uno/warocol.com/issues/337
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                table_row = await conn.fetchrow(
                    "SELECT id, is_bar FROM tables WHERE id = $1 AND tenant_id = $2 AND is_active = true FOR UPDATE",
                    table_id,
                    tenant_id,
                )
                if not table_row:
                    raise NotFoundError("Table not found")

                if table_row["is_bar"]:
                    raise APIError("La Barra no puede ser descartada", status_code=409)

                session_row = await conn.fetchrow(
                    "SELECT id FROM table_sessions WHERE table_id = $1 AND tenant_id = $2 AND closed_at IS NULL",
                    table_id,
                    tenant_id,
                )
                if not session_row:
                    raise NotFoundError("No open session found for this table")

                session_id = session_row["id"]

                # Guard: cannot discard a session that has completed orders
                completed_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM orders WHERE table_session_id = $1 AND status = 'completed'",
                    session_id,
                )
                if completed_count and completed_count > 0:
                    raise APIError(
                        "No se puede descartar una sesión con órdenes completadas",
                        status_code=409,
                    )

                # Hard-delete pending orders (cascade: modifiers → items → orders)
                await conn.execute(
                    """
                    DELETE FROM order_item_modifiers
                    WHERE order_item_id IN (
                        SELECT oi.id FROM order_items oi
                        JOIN orders o ON o.id = oi.order_id
                        WHERE o.table_session_id = $1 AND o.status = 'pending'
                    )
                    """,
                    session_id,
                )
                await conn.execute(
                    """
                    DELETE FROM order_items
                    WHERE order_id IN (
                        SELECT id FROM orders
                        WHERE table_session_id = $1 AND status = 'pending'
                    )
                    """,
                    session_id,
                )
                await conn.execute(
                    "DELETE FROM orders WHERE table_session_id = $1 AND status = 'pending'",
                    session_id,
                )

                # Soft-close and mark as discarded
                await conn.execute(
                    "UPDATE table_sessions SET is_discarded = TRUE, closed_at = now() WHERE id = $1",
                    session_id,
                )

                # Reset table status
                await conn.execute(
                    "UPDATE tables SET status = 'free' WHERE id = $1 AND tenant_id = $2",
                    table_id,
                    tenant_id,
                )

        logger.info(f"[discard_table_session] Session {session_id} discarded for table {table_id}")
        return {
            "success": True,
            "data": {
                "session_id": str(session_id),
                "table_id": str(table_id),
            },
        }

    except (AuthenticationError, NotFoundError, APIError):
        raise
    except Exception as e:
        logger.error(f"Error discarding session for table {table_id}: {e}")
        raise APIError(f"Error discarding session: {e}", status_code=500)


async def reopen_table_session(request: Request, table_id: UUID) -> dict:
    """
    Reopen the most recent non-discarded closed session for a table.
    Sets closed_at = NULL and restores table status to 'open'.
    Returns 409 for bar table or if table already has an open session.
    Returns 404 if no closed session exists to reopen.

    Issue: https://github.com/uno0uno/warocol.com/issues/337
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                table_row = await conn.fetchrow(
                    "SELECT id, is_bar, status FROM tables WHERE id = $1 AND tenant_id = $2 AND is_active = true FOR UPDATE",
                    table_id,
                    tenant_id,
                )
                if not table_row:
                    raise NotFoundError("Table not found")

                if table_row["is_bar"]:
                    raise APIError("La Barra no puede reabrir sesiones", status_code=409)

                if table_row["status"] == "open":
                    raise APIError("La mesa ya tiene una sesión abierta", status_code=409)

                # Find the most recent non-discarded closed session
                closed_session = await conn.fetchrow(
                    """
                    SELECT id, closed_at
                    FROM table_sessions
                    WHERE table_id = $1
                      AND tenant_id = $2
                      AND closed_at IS NOT NULL
                      AND is_discarded = FALSE
                    ORDER BY closed_at DESC
                    LIMIT 1
                    """,
                    table_id,
                    tenant_id,
                )
                if not closed_session:
                    raise NotFoundError("No hay sesión cerrada para reabrir")

                session_id = closed_session["id"]

                # Reopen: clear closed_at
                await conn.execute(
                    "UPDATE table_sessions SET closed_at = NULL WHERE id = $1",
                    session_id,
                )

                # Restore table status to open
                await conn.execute(
                    "UPDATE tables SET status = 'open' WHERE id = $1 AND tenant_id = $2",
                    table_id,
                    tenant_id,
                )

        logger.info(f"[reopen_table_session] Session {session_id} reopened for table {table_id}")
        return {
            "success": True,
            "data": {
                "session_id": str(session_id),
                "table_id": str(table_id),
            },
        }

    except (AuthenticationError, NotFoundError, APIError):
        raise
    except Exception as e:
        logger.error(f"Error reopening session for table {table_id}: {e}")
        raise APIError(f"Error reopening session: {e}", status_code=500)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _format_table_row(row: dict) -> dict:
    result = {
        "id": str(row["id"]),
        "name": row["name"],
        "capacity": row["capacity"],
        "status": row["status"],
        "is_active": row["is_active"],
        "is_bar": bool(row["is_bar"]) if row.get("is_bar") is not None else False,
        "created_at": row["created_at"].isoformat(),
        "session": None,
        "last_closed_at": row["last_closed_at"].isoformat() if row.get("last_closed_at") else None,
        "last_closed_session_id": str(row["last_closed_session_id"]) if row.get("last_closed_session_id") else None,
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

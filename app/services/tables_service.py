"""
Tables Service
Business logic for table management and session lifecycle.

Issue: https://github.com/uno0uno/warocol.com/issues/298
"""
import secrets
from typing import Optional, List
from uuid import UUID
from datetime import date
from decimal import Decimal
from zoneinfo import ZoneInfo
_BOG = ZoneInfo("America/Bogota")
from fastapi import Request
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError, APIError, NotFoundError
from app.services.cierre_service import _get_tenant_tax_config, _post_order_gl_entry, _post_order_cogs_gl_entry
from app.services.accounting_service import void_order_journal_entry_in_txn
from app.services.pos_cart_service import _capture_order_item_ingredients, _PAYMENT_VOID_ROLES
from app.services.orders_service import _compute_tax_breakdown
from app.services.ingredient_purchase_units_service import resolve_recipe_quantity_to_base_unit
from app.services.comandas_service import fire_comandas
import logging

logger = logging.getLogger(__name__)

_QR_TOKEN_URLSAFE_BYTES = 32
_QR_TOKEN_MAX_ATTEMPTS = 5


async def _generate_unique_qr_token(conn) -> str:
    """Opaque token for public table QR URLs (api-warolabs#266)."""
    for _ in range(_QR_TOKEN_MAX_ATTEMPTS):
        token = secrets.token_urlsafe(_QR_TOKEN_URLSAFE_BYTES)
        exists = await conn.fetchval(
            "SELECT 1 FROM tables WHERE qr_public_token = $1",
            token,
        )
        if not exists:
            return token
    raise APIError("No se pudo generar un token QR único", status_code=500)


async def _require_table_qr_module(conn, tenant_id) -> None:
    enabled = await conn.fetchval(
        """
        SELECT table_qr_module_enabled
        FROM tenant_public_profiles
        WHERE tenant_id = $1
        """,
        tenant_id,
    )
    if not enabled:
        raise APIError(
            "El módulo de pedido por QR en mesa no está habilitado",
            status_code=409,
        )


async def _get_table_for_qr_management(conn, tenant_id, table_id) -> dict:
    row = await conn.fetchrow(
        """
        SELECT id, name, is_bar, is_active, deleted_at, qr_enabled, qr_public_token
        FROM tables
        WHERE id = $1 AND tenant_id = $2
        """,
        table_id,
        tenant_id,
    )
    if not row or row["deleted_at"]:
        raise NotFoundError("Table not found")
    if row["is_bar"]:
        raise APIError("La Barra no admite pedidos por QR", status_code=409)
    if not row["is_active"]:
        raise APIError("No se puede configurar QR en una mesa inactiva", status_code=409)
    return dict(row)


def _format_table_qr_fields(row: dict) -> dict:
    return {
        "qr_enabled": bool(row.get("qr_enabled")) if row.get("qr_enabled") is not None else False,
        "qr_public_token": row.get("qr_public_token"),
    }


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


async def list_tables(request: Request, include_inactive: bool = False) -> dict:
    """
    List tables for the tenant.
    When include_inactive=True (admin page), also returns deactivated (is_active=false)
    tables that have not been permanently deleted (deleted_at IS NULL).
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
                    t.qr_enabled,
                    t.qr_public_token,
                    t.created_at,
                    t.assigned_member_id,
                    p_assigned.name AS assigned_member_name,
                    tm_assigned.role AS assigned_member_role,
                    ts.id            AS session_id,
                    ts.opened_at,
                    ts.opened_by_user_id,
                    ts.attended_by_member_id AS session_attended_by_member_id,
                    p_attended.name AS session_attended_by_member_name,
                    tm_attended.role AS session_attended_by_member_role,
                    -- Resolver: session override > table default > NULL
                    COALESCE(ts.attended_by_member_id, t.assigned_member_id) AS effective_waiter_member_id,
                    COALESCE(p_attended.name, p_assigned.name)               AS effective_waiter_member_name,
                    COALESCE(tm_attended.role, tm_assigned.role)             AS effective_waiter_member_role,
                    EXTRACT(EPOCH FROM (now() - ts.opened_at)) / 60 AS session_duration_minutes,
                    COALESCE(
                        (SELECT SUM(o.total_amount)
                         FROM orders o
                         WHERE o.table_session_id = ts.id),
                        0
                    ) AS running_total,
                    COALESCE(
                        (SELECT COUNT(*)
                         FROM orders o2
                         JOIN order_items oi ON oi.order_id = o2.id
                         WHERE o2.table_session_id = ts.id
                           AND oi.fulfillment_status = 'new'),
                        0
                    ) AS unfired_count,
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
                LEFT JOIN tenant_members tm_assigned
                    ON tm_assigned.id = t.assigned_member_id
                LEFT JOIN profile p_assigned
                    ON p_assigned.id = tm_assigned.user_id
                LEFT JOIN tenant_members tm_attended
                    ON tm_attended.id = ts.attended_by_member_id
                LEFT JOIN profile p_attended
                    ON p_attended.id = tm_attended.user_id
                WHERE t.tenant_id = $1
                  AND t.deleted_at IS NULL
                  AND (t.is_active = true OR $2)
                ORDER BY t.is_bar DESC, t.is_active DESC, t.name
                """,
                tenant_id,
                include_inactive,
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
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    INSERT INTO tables (tenant_id, name, capacity)
                    VALUES ($1, $2, $3)
                    RETURNING id, name, capacity, status, is_active, is_bar,
                              qr_enabled, qr_public_token, created_at
                    """,
                    tenant_id,
                    name,
                    capacity,
                )
                module_on = await conn.fetchval(
                    """
                    SELECT table_qr_module_enabled
                    FROM tenant_public_profiles
                    WHERE tenant_id = $1
                    """,
                    tenant_id,
                )
                if module_on and not row["is_bar"]:
                    token = await _generate_unique_qr_token(conn)
                    row = await conn.fetchrow(
                        """
                        UPDATE tables
                        SET qr_public_token = $1
                        WHERE id = $2 AND tenant_id = $3
                        RETURNING id, name, capacity, status, is_active, is_bar,
                                  qr_enabled, qr_public_token, created_at
                        """,
                        token,
                        row["id"],
                        tenant_id,
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


async def activate_table(request: Request, table_id: UUID) -> dict:
    """
    Re-activate a deactivated table (is_active = true).
    Returns 409 if table is permanent-deleted (deleted_at IS NOT NULL) or is bar.
    Issue: https://github.com/uno0uno/warocol.com/issues/436
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            existing = await conn.fetchrow(
                "SELECT id, is_bar, is_active, deleted_at FROM tables WHERE id = $1 AND tenant_id = $2",
                table_id,
                tenant_id,
            )
            if not existing:
                raise NotFoundError("Table not found")

            if existing["is_bar"]:
                raise APIError("La Barra no puede ser modificada", status_code=409)

            if existing["deleted_at"] is not None:
                raise APIError("Esta mesa ha sido eliminada permanentemente", status_code=409)

            if existing["is_active"]:
                return {"success": True, "message": "Table already active"}

            await conn.execute(
                "UPDATE tables SET is_active = true WHERE id = $1 AND tenant_id = $2",
                table_id,
                tenant_id,
            )

        logger.info(f"Table activated: {table_id}")
        return {"success": True, "message": "Table activated"}

    except (AuthenticationError, NotFoundError, APIError):
        raise
    except Exception as e:
        logger.error(f"Error activating table {table_id}: {e}")
        raise APIError(f"Error activating table: {e}", status_code=500)


async def deactivate_table(request: Request, table_id: UUID) -> dict:
    """
    Temporarily deactivate a table (is_active = false).
    Returns 409 if table has an open session or is bar.
    Issue: https://github.com/uno0uno/warocol.com/issues/436
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            existing = await conn.fetchrow(
                "SELECT id, is_bar FROM tables WHERE id = $1 AND tenant_id = $2 AND is_active = true AND deleted_at IS NULL",
                table_id,
                tenant_id,
            )
            if not existing:
                raise NotFoundError("Table not found or already inactive")

            if existing["is_bar"]:
                raise APIError("La Barra no puede ser desactivada", status_code=409)

            open_session = await conn.fetchrow(
                "SELECT id FROM table_sessions WHERE table_id = $1 AND tenant_id = $2 AND closed_at IS NULL",
                table_id,
                tenant_id,
            )
            if open_session:
                raise APIError("Esta mesa tiene una sesión activa. Ciérrala antes de desactivarla.", status_code=409)

            await conn.execute(
                "UPDATE tables SET is_active = false WHERE id = $1 AND tenant_id = $2",
                table_id,
                tenant_id,
            )

        logger.info(f"Table deactivated: {table_id}")
        return {"success": True, "message": "Table deactivated"}

    except (AuthenticationError, NotFoundError, APIError):
        raise
    except Exception as e:
        logger.error(f"Error deactivating table {table_id}: {e}")
        raise APIError(f"Error deactivating table: {e}", status_code=500)


async def delete_table_permanent(request: Request, table_id: UUID) -> dict:
    """
    Permanently remove a table.
    - Open session → 409 (cannot delete)
    - No closed history → hard DELETE from DB
    - Has closed history → soft-archive (deleted_at = now(), is_active = false)
    Issue: https://github.com/uno0uno/warocol.com/issues/436
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            existing = await conn.fetchrow(
                "SELECT id, is_bar, deleted_at FROM tables WHERE id = $1 AND tenant_id = $2",
                table_id,
                tenant_id,
            )
            if not existing:
                raise NotFoundError("Table not found")

            if existing["is_bar"]:
                raise APIError("La Barra no puede ser eliminada", status_code=409)

            if existing["deleted_at"] is not None:
                raise APIError("Esta mesa ya ha sido eliminada", status_code=409)

            open_session = await conn.fetchrow(
                "SELECT id FROM table_sessions WHERE table_id = $1 AND tenant_id = $2 AND closed_at IS NULL",
                table_id,
                tenant_id,
            )
            if open_session:
                raise APIError("Mesa con sesión activa. Cierra la sesión antes de eliminar.", status_code=409)

            has_history = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM table_sessions WHERE table_id = $1 AND tenant_id = $2 AND closed_at IS NOT NULL AND is_discarded = FALSE)",
                table_id,
                tenant_id,
            )

            if has_history:
                await conn.execute(
                    "UPDATE tables SET deleted_at = now(), is_active = false WHERE id = $1 AND tenant_id = $2",
                    table_id,
                    tenant_id,
                )
                logger.info(f"Table archived (soft-deleted): {table_id}")
                return {"success": True, "message": "Table archived", "archived": True}
            else:
                await conn.execute(
                    "DELETE FROM tables WHERE id = $1 AND tenant_id = $2",
                    table_id,
                    tenant_id,
                )
                logger.info(f"Table hard-deleted: {table_id}")
                return {"success": True, "message": "Table deleted", "archived": False}

    except (AuthenticationError, NotFoundError, APIError):
        raise
    except Exception as e:
        logger.error(f"Error permanently deleting table {table_id}: {e}")
        raise APIError(f"Error deleting table: {e}", status_code=500)


async def open_session(
    request: Request,
    table_id: UUID,
    attended_by_member_id: Optional[UUID] = None,
) -> dict:
    """
    Open a new session for a table.

    Optional `attended_by_member_id` (warocol.com#574) lets the cashier
    pre-set the per-session waiter override at open time. If the tenant's
    `waiter_attribution_enabled` flag is off OR the value is None, the
    field is left NULL on the new row and the resolver falls back to
    `tables.assigned_member_id`.

    Returns 409 if a session is already open. Returns 404 if the table
    doesn't exist or if `attended_by_member_id` doesn't belong to the
    tenant.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        # If a waiter override was requested, check the toggle is ON and
        # the member belongs to the tenant. Done OUTSIDE the table lock
        # to fail fast.
        resolved_attended_by: Optional[UUID] = None
        if attended_by_member_id is not None:
            async with get_db_connection(use_transaction=False) as conn:
                toggle = await conn.fetchval(
                    "SELECT waiter_attribution_enabled FROM tenant_public_profiles WHERE tenant_id = $1",
                    tenant_id,
                )
                if toggle:
                    member_check = await conn.fetchval(
                        """
                        SELECT id FROM tenant_members
                        WHERE id = $1 AND tenant_id = $2 AND is_active = true AND terminated_at IS NULL
                        """,
                        attended_by_member_id,
                        tenant_id,
                    )
                    if member_check is None:
                        raise NotFoundError("Member not found")
                    resolved_attended_by = attended_by_member_id
                # If toggle OFF: silently ignore the field (idempotent).

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
                    INSERT INTO table_sessions (table_id, tenant_id, opened_by_user_id, attended_by_member_id)
                    VALUES ($1, $2, $3, $4)
                    RETURNING id, opened_at
                    """,
                    table_id,
                    tenant_id,
                    user_id,
                    resolved_attended_by,
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
                "attended_by_member_id": str(resolved_attended_by) if resolved_attended_by else None,
            },
        }

    except (AuthenticationError, NotFoundError, APIError):
        raise
    except Exception as e:
        logger.error(f"Error opening session for table {table_id}: {e}")
        raise APIError(f"Error opening session: {e}", status_code=500)


async def close_session(request: Request, table_id: UUID, payment_method: Optional[str] = None, customer_id: Optional[str] = None, credit_due_date: Optional[date] = None, payment_method_id: Optional[UUID] = None, discount_type: Optional[str] = None, discount_value: Optional[float] = None, split_mode: bool = False, split_first_amount: float = 0.0, *, split_first_cash_received: Optional[float] = None, cash_received: Optional[float] = None, tip_amount: float = 0, tip_source: str = 'none', served_by_member_id: Optional[UUID] = None) -> dict:
    """
    Close the active session for a table.
    If payment_method is provided, marks all pending orders as completed with that payment method.
    """
    # warocol.com#639 — tip validation (same rules as pos_cart.complete_pos_order)
    if tip_amount < 0:
        raise APIError("tip_amount must be non-negative", status_code=400)
    if tip_source not in ('preset', 'custom', 'none'):
        raise APIError(f"invalid tip_source: {tip_source!r}", status_code=400)
    if tip_amount == 0:
        tip_source = 'none'
    elif tip_source == 'none':
        raise APIError("tip_source cannot be 'none' when tip_amount > 0", status_code=400)
    if tip_amount > 0 and split_mode:
        raise APIError("Tip is not supported in split-payment mode in this phase", status_code=400)

    resolved_served_by: Optional[UUID] = None
    if served_by_member_id is not None:
        session_context_pre = require_valid_session(request)
        tenant_id_pre = session_context_pre.tenant_id
        if not tenant_id_pre:
            raise AuthenticationError("Tenant ID is required")
        async with get_db_connection(use_transaction=False) as _conn:
            member_check = await _conn.fetchval(
                """
                SELECT id FROM tenant_members
                WHERE id = $1 AND tenant_id = $2 AND is_active = true AND terminated_at IS NULL
                """,
                served_by_member_id,
                tenant_id_pre,
            )
            if member_check is None:
                raise NotFoundError("Member not found")
            resolved_served_by = served_by_member_id

    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                table_row = await conn.fetchrow(
                    "SELECT id, is_bar, name FROM tables WHERE id = $1 AND tenant_id = $2 AND is_active = true FOR UPDATE",
                    table_id,
                    tenant_id,
                )
                if not table_row:
                    raise NotFoundError("Table not found")

                session_row = await conn.fetchrow(
                    """
                    SELECT
                        ts.id,
                        COALESCE(ts.attended_by_member_id, t.assigned_member_id) AS effective_waiter_member_id
                    FROM table_sessions ts
                    JOIN tables t ON t.id = ts.table_id
                    WHERE ts.table_id = $1 AND ts.tenant_id = $2 AND ts.closed_at IS NULL
                    """,
                    table_id,
                    tenant_id,
                )
                if not session_row:
                    raise NotFoundError("No open session found for this table")

                # warocol.com#665 — when checkout omits served_by (session already has a waiter),
                # copy the effective waiter onto completed orders for propinas / member reports.
                if resolved_served_by is None and session_row.get("effective_waiter_member_id"):
                    member_ok = await conn.fetchval(
                        """
                        SELECT id FROM tenant_members
                        WHERE id = $1 AND tenant_id = $2 AND is_active = true AND terminated_at IS NULL
                        """,
                        session_row["effective_waiter_member_id"],
                        tenant_id,
                    )
                    if member_ok:
                        resolved_served_by = session_row["effective_waiter_member_id"]

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

                    payment_status = 'credit' if payment_method == 'credit' else ('partial' if split_mode else 'paid')

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

                    # warocol.com#663 — checkout waiter attribution on all completed session orders
                    if resolved_served_by is not None:
                        await conn.execute(
                            """
                            UPDATE orders
                            SET served_by_member_id = $1, updated_at = now()
                            WHERE table_session_id = $2 AND status = 'completed'
                            """,
                            resolved_served_by,
                            session_row["id"],
                        )

                    # Issue #524 — single-payment (non-split) cash close: attach cash_received
                    # to the FIRST completed order in the session (others stay NULL). Cierre
                    # SUM(cash_received) over a period reconstructs total cash received correctly.
                    if not split_mode and cash_received is not None:
                        if payment_method != 'cash':
                            raise APIError("cash_received solo aplica a pagos en efectivo", status_code=400)
                        session_total_for_check = await conn.fetchval(
                            "SELECT COALESCE(SUM(total_amount), 0) FROM orders WHERE table_session_id = $1 AND status = 'completed'",
                            session_row["id"],
                        )
                        if cash_received < float(session_total_for_check or 0):
                            raise APIError(
                                f"Efectivo recibido ({cash_received}) debe ser mayor o igual al total de la sesión ({session_total_for_check})",
                                status_code=400,
                            )
                        await conn.execute(
                            """
                            UPDATE orders SET cash_received = $1
                             WHERE id = (
                               SELECT id FROM orders
                                WHERE table_session_id = $2 AND status = 'completed'
                                ORDER BY created_at LIMIT 1
                             )
                            """,
                            cash_received,
                            session_row["id"],
                        )

                    # warocol.com#639 — apply session-level tip to the first completed order
                    # (same single-row pattern as cash_received). The tip lives on one
                    # `orders` row so the /ventas/propinas aggregator sees a single entry
                    # per mesa close instead of N inflated percentages.
                    if not split_mode and tip_amount > 0:
                        tenant_tip_enabled = await conn.fetchval(
                            "SELECT tip_enabled FROM tenant_public_profiles WHERE tenant_id = $1",
                            tenant_id,
                        )
                        if not bool(tenant_tip_enabled):
                            raise APIError("Tipping is not enabled for this tenant", status_code=400)
                        if payment_method == 'cash' and cash_received is not None:
                            session_total_with_tip = await conn.fetchval(
                                "SELECT COALESCE(SUM(total_amount), 0) FROM orders WHERE table_session_id = $1 AND status = 'completed'",
                                session_row["id"],
                            )
                            if cash_received < (float(session_total_with_tip or 0) + float(tip_amount)):
                                raise APIError(
                                    f"Efectivo recibido ({cash_received}) debe cubrir total + propina ({float(session_total_with_tip or 0) + float(tip_amount)})",
                                    status_code=400,
                                )
                        await conn.execute(
                            """
                            UPDATE orders SET tip_amount = $1, tip_source = $2
                             WHERE id = (
                               SELECT id FROM orders
                                WHERE table_session_id = $3 AND status = 'completed'
                                ORDER BY created_at LIMIT 1
                             )
                            """,
                            float(tip_amount),
                            tip_source,
                            session_row["id"],
                        )

                    # GL journal entries — one per order, atomic with session close
                    # Failure is swallowed: GL must never block the close
                    try:
                        completed_orders = await conn.fetch(
                            "SELECT id, order_number, total_amount, payment_method, payment_method_id, order_date "
                            "FROM orders WHERE table_session_id = $1 AND status = 'completed'",
                            session_row["id"],
                        )
                        tax_config = await _get_tenant_tax_config(conn, tenant_id)
                        for ord_row in completed_orders:
                            await _post_order_gl_entry(
                                conn=conn,
                                tenant_id=tenant_id,
                                order_id=ord_row["id"],
                                order_date=ord_row["order_date"].astimezone(_BOG).date(),
                                total_amount=Decimal(str(ord_row["total_amount"])),
                                payment_method=ord_row["payment_method"] or payment_method or "digital",
                                payment_method_id=ord_row["payment_method_id"],
                                tax_config=tax_config,
                                order_number=int(ord_row["order_number"]),
                            )
                            await _post_order_cogs_gl_entry(
                                conn=conn,
                                tenant_id=tenant_id,
                                order_id=ord_row["id"],
                                order_date=ord_row["order_date"].astimezone(_BOG).date(),
                                order_number=int(ord_row["order_number"]),
                            )
                    except Exception as _gl_exc:
                        logger.error(f"GL entries failed for session {session_row['id']}: {_gl_exc}")

                    # In split_mode: record first payment proportionally across session orders
                    # and keep session open.
                    # Issue #524: validate split_first_cash_received once, then attach the full
                    # cash_received to the FIRST order's row only (others stay NULL). Cierre sums
                    # cash_received over the period — putting it once is correct and avoids the
                    # CHECK (cash_received >= amount) constraint violating on proportionally-split rows.
                    _split_first_payment_id_mesa: Optional[str] = None
                    if split_mode and split_first_amount > 0:
                        if split_first_cash_received is not None:
                            if payment_method != 'cash':
                                raise APIError("split_first_cash_received solo aplica a pagos en efectivo", status_code=400)
                            if split_first_cash_received < split_first_amount:
                                raise APIError(
                                    f"Efectivo recibido ({split_first_cash_received}) debe ser mayor o igual al monto ({split_first_amount})",
                                    status_code=400,
                                )
                        user_id = session_context.user_id if hasattr(session_context, 'user_id') else None
                        order_rows = await conn.fetch(
                            "SELECT id, total_amount FROM orders WHERE table_session_id = $1 AND status = 'completed' ORDER BY created_at",
                            session_row["id"],
                        )
                        session_total = sum(float(r["total_amount"]) for r in order_rows)
                        if session_total > 0 and order_rows:
                            remaining_payment = split_first_amount
                            for i, ord_row in enumerate(order_rows):
                                if i == len(order_rows) - 1:
                                    portion = remaining_payment
                                else:
                                    portion = round(split_first_amount * float(ord_row["total_amount"]) / session_total)
                                    remaining_payment -= portion
                                # Issue #524: cash_received only on the first row (sum still correct over period)
                                row_cash_received = split_first_cash_received if i == 0 else None
                                # Issue warocol.com#649 — RETURNING id so the frontend has a
                                # real UUID for void operations (siblings resolved via heuristic).
                                inserted_row = await conn.fetchrow(
                                    """
                                    INSERT INTO order_payments
                                        (order_id, tenant_id, amount, payment_method, payment_method_id, created_by_user_id, cash_received)
                                    VALUES ($1, $2, $3, $4, $5::uuid, $6::uuid, $7)
                                    RETURNING id
                                    """,
                                    ord_row["id"], tenant_id, portion, payment_method,
                                    str(payment_method_id) if payment_method_id else None,
                                    str(user_id) if user_id else None,
                                    row_cash_received,
                                )
                                if i == 0:
                                    _split_first_payment_id_mesa = str(inserted_row["id"])

                else:
                    completed_count = 0

                pending_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM orders WHERE table_session_id = $1 AND status = 'pending'",
                    session_row["id"],
                )

                if not split_mode:
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

                # Auto-fire hook: if comandas enabled, fire all 'new' items for this session
                # This ensures any items added but not explicitly fired get sent at checkout
                try:
                    _prof = await conn.fetchrow(
                        "SELECT comandas_enabled FROM tenant_public_profiles WHERE tenant_id = $1",
                        tenant_id
                    )
                    if _prof and _prof["comandas_enabled"]:
                        # Find all pending orders for this session (they might have been completed just above)
                        # We need to fire them. fire_comandas handles 'new' status check.
                        session_orders = await conn.fetch(
                            "SELECT id FROM orders WHERE table_session_id = $1",
                            session_row["id"]
                        )
                        for _order in session_orders:
                            await fire_comandas(
                                order_id=_order["id"],
                                tenant_id=tenant_id,
                                source_type='table',
                                table_display_name=table_row["name"],
                                conn=conn
                            )

                        # Auto-deliver: mark all non-terminal comandas as delivered when session closes
                        session_order_ids = [_o["id"] for _o in session_orders]
                        await conn.execute("""
                            UPDATE comandas
                            SET status = 'delivered', delivered_at = NOW(), updated_at = NOW()
                            WHERE order_id = ANY($1::uuid[])
                              AND tenant_id = $2
                              AND status IN ('pending', 'preparing', 'ready')
                        """, session_order_ids, tenant_id)
                except Exception as _fe:
                    logger.error(f"Auto-fire failed during close_session for table {table_id}: {_fe}")

        if split_mode:
            # Compute paid_total and remaining for the split response
            async with get_db_connection() as conn2:
                order_rows = await conn2.fetch(
                    "SELECT id, total_amount FROM orders WHERE table_session_id = $1",
                    session_row["id"],
                )
                session_total = sum(float(r["total_amount"]) for r in order_rows)
                order_ids = [r["id"] for r in order_rows]
                paid_row = await conn2.fetchrow(
                    "SELECT COALESCE(SUM(amount), 0) AS paid FROM order_payments WHERE order_id = ANY($1) AND voided_at IS NULL",
                    order_ids,
                )
                paid_total = float(paid_row["paid"])
                remaining = max(0.0, session_total - paid_total)
                is_complete = remaining <= 0.01
            logger.info(f"Split payment recorded for session {session_row['id']}: paid={paid_total}, remaining={remaining}")
            return {
                "success": True,
                "data": {
                    "session_id": str(session_row["id"]),
                    "table_id": str(table_id),
                    "paid_total": paid_total,
                    "remaining": remaining,
                    "is_complete": is_complete,
                    # Issue warocol.com#649 — real UUID of the first inserted row.
                    "payment_id": _split_first_payment_id_mesa,
                },
            }

        # Fetch completed order IDs (+ numbers) for downstream flows (invoice, receipts).
        # Fresh connection — main conn may be released.
        async with get_db_connection() as conn_ids:
            order_rows = await conn_ids.fetch(
                """
                SELECT id, order_number, total_amount
                FROM orders
                WHERE table_session_id = $1 AND status = 'completed'
                ORDER BY created_at
                """,
                session_row["id"],
            )
            # Tax breakdown for the whole mesa close (aggregated across completed orders)
            _std_tax = 0.0
            _liq_tax = 0.0
            _tax_label = "Impuesto"
            try:
                tax_config = await _get_tenant_tax_config(conn_ids, tenant_id)
                tax_rows = await conn_ids.fetch(
                    """
                    SELECT
                        COALESCE(p.tax_category, 'standard') AS tax_category,
                        COALESCE(SUM(oi.subtotal), 0) AS subtotal
                    FROM order_items oi
                    JOIN orders o ON o.id = oi.order_id
                    JOIN product p ON p.id = oi.product_id
                    WHERE o.id = ANY($1::uuid[])
                    GROUP BY COALESCE(p.tax_category, 'standard')
                    """,
                    [r["id"] for r in order_rows],
                )
                _std_tax, _liq_tax, _tax_label = _compute_tax_breakdown(tax_rows, tax_config)
            except Exception as _e:
                logger.warning(f"Tax breakdown failed for mesa close (table {table_id}): {_e}")
        order_ids = [str(r["id"]) for r in order_rows]
        order_numbers = [int(r["order_number"]) for r in order_rows if r["order_number"] is not None]

        # Defensive: keep alignment even if order_number is unexpectedly null
        if len(order_numbers) != len(order_ids):
            order_numbers = [
                int(r["order_number"]) if r["order_number"] is not None else 0
                for r in order_rows
            ]

        logger.info(f"Session closed: {session_row['id']} for table {table_id} ({len(order_ids)} orders)")
        return {
            "success": True,
            "data": {
                "session_id": str(session_row["id"]),
                "table_id": str(table_id),
                "completed_orders": int(completed_count or 0),
                "pending_orders": int(pending_count),
                "order_ids": order_ids,
                "order_numbers": order_numbers,
                **({"order_number": order_numbers[0]} if len(order_numbers) == 1 else {}),
                "standard_tax": float(_std_tax),
                "liquor_tax": float(_liq_tax),
                "standard_tax_label": _tax_label,
                "tip_amount": float(tip_amount) if tip_amount > 0 else 0,
                "tip_source": tip_source,
                "charged_amount": float(sum(float(r.get("total_amount", 0)) for r in order_rows) + float(tip_amount)) if tip_amount > 0 else None,
            },
        }

    except (AuthenticationError, NotFoundError, APIError):
        raise
    except Exception as e:
        logger.error(f"Error closing session for table {table_id}: {e}")
        raise APIError(f"Error closing session: {e}", status_code=500)


async def add_session_payment(
    request: Request,
    table_id: UUID,
    amount: float,
    payment_method: str,
    payment_method_id: Optional[UUID] = None,
    cash_received: Optional[float] = None,
) -> dict:
    """
    Add a partial payment to an open mesa session that is in split payment mode.
    Distributes the payment proportionally across session orders.
    When total paid >= session total, closes the session and marks all orders as paid.

    Issue #524: cash_received captures the bill amount handed by the customer for
    cash payment lines. Stored on the FIRST proportional row only (NULL on the
    rest); cierre SUM(cash_received) over a period yields the correct total.
    """
    # Issue #524 — defense-in-depth validation
    if cash_received is not None:
        if payment_method != 'cash':
            raise APIError("cash_received solo aplica a pagos en efectivo", status_code=400)
        if cash_received < amount:
            raise APIError(
                f"Efectivo recibido ({cash_received}) debe ser mayor o igual al monto ({amount})",
                status_code=400,
            )
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        user_id = session_context.user_id if hasattr(session_context, 'user_id') else None

        async with get_db_connection() as conn:
            async with conn.transaction():
                # Get open session
                session_row = await conn.fetchrow(
                    "SELECT id FROM table_sessions WHERE table_id = $1 AND tenant_id = $2 AND closed_at IS NULL FOR UPDATE",
                    table_id, tenant_id,
                )
                if not session_row:
                    raise NotFoundError("No open session found for this table")

                # Get all completed (partial) orders for this session
                order_rows = await conn.fetch(
                    "SELECT id, total_amount FROM orders WHERE table_session_id = $1 AND status = 'completed' ORDER BY created_at",
                    session_row["id"],
                )
                if not order_rows:
                    raise APIError("No split payment orders found for this session — call close with split_mode=True first", status_code=400)

                session_total = sum(float(r["total_amount"]) for r in order_rows)
                order_ids = [r["id"] for r in order_rows]

                # Distribute this payment proportionally
                remaining_payment = amount
                first_payment_id: Optional[str] = None
                for i, ord_row in enumerate(order_rows):
                    if i == len(order_rows) - 1:
                        portion = remaining_payment
                    else:
                        portion = round(amount * float(ord_row["total_amount"]) / session_total)
                        remaining_payment -= portion
                    # Issue #524 — cash_received only on the first row (sum still correct over period)
                    row_cash_received = cash_received if i == 0 else None
                    # Issue warocol.com#649 — RETURNING id so the frontend has a
                    # real UUID for void operations (was: "split" placeholder → 422).
                    inserted_row = await conn.fetchrow(
                        """
                        INSERT INTO order_payments
                            (order_id, tenant_id, amount, payment_method, payment_method_id, created_by_user_id, cash_received)
                        VALUES ($1, $2, $3, $4, $5::uuid, $6::uuid, $7)
                        RETURNING id
                        """,
                        ord_row["id"], tenant_id, portion, payment_method,
                        str(payment_method_id) if payment_method_id else None,
                        str(user_id) if user_id else None,
                        row_cash_received,
                    )
                    if i == 0:
                        first_payment_id = str(inserted_row["id"])

                # Recompute paid total
                paid_row = await conn.fetchrow(
                    "SELECT COALESCE(SUM(amount), 0) AS paid FROM order_payments WHERE order_id = ANY($1) AND voided_at IS NULL",
                    order_ids,
                )
                paid_total = float(paid_row["paid"])
                remaining = max(0.0, session_total - paid_total)
                is_complete = remaining <= 0.01

                if is_complete:
                    # Mark all orders as fully paid
                    await conn.execute(
                        "UPDATE orders SET payment_status = 'paid' WHERE table_session_id = $1 AND status = 'completed' AND payment_status = 'partial'",
                        session_row["id"],
                    )
                    # Close the session
                    await conn.execute(
                        "UPDATE table_sessions SET closed_at = now() WHERE id = $1",
                        session_row["id"],
                    )
                    # Reset table to free
                    await conn.execute(
                        "UPDATE tables SET status = 'free' WHERE id = $1 AND tenant_id = $2",
                        table_id, tenant_id,
                    )
                    logger.info(f"Session {session_row['id']} closed via split payment (fully paid)")

        return {
            "success": True,
            "data": {
                "session_id": str(session_row["id"]),
                "paid_total": paid_total,
                "remaining": remaining,
                "is_complete": is_complete,
                # Issue warocol.com#649 — real UUID; siblings void via heuristic.
                "payment_id": first_payment_id,
            },
        }

    except (AuthenticationError, NotFoundError, APIError):
        raise
    except Exception as e:
        logger.error(f"Error adding session payment for table {table_id}: {e}")
        raise APIError(f"Error adding session payment: {e}", status_code=500)


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
                    ts.attended_by_member_id,
                    p_attended.name AS attended_by_member_name,
                    tm_attended.role AS attended_by_member_role,
                    -- Resolver: session override > table default > NULL
                    COALESCE(ts.attended_by_member_id, t.assigned_member_id) AS effective_waiter_member_id,
                    COALESCE(p_attended.name, p_assigned.name)               AS effective_waiter_member_name,
                    COALESCE(tm_attended.role, tm_assigned.role)             AS effective_waiter_member_role,
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
                JOIN tables t ON t.id = ts.table_id
                LEFT JOIN tenant_members tm_attended
                    ON tm_attended.id = ts.attended_by_member_id
                LEFT JOIN profile p_attended
                    ON p_attended.id = tm_attended.user_id
                LEFT JOIN tenant_members tm_assigned
                    ON tm_assigned.id = t.assigned_member_id
                LEFT JOIN profile p_assigned
                    ON p_assigned.id = tm_assigned.user_id
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

            # Tax breakdown preview for UI (mesa checkout summary)
            _std_tax = 0.0
            _liq_tax = 0.0
            _tax_label = "Impuesto"
            try:
                from app.services.orders_service import _compute_tax_breakdown

                tax_config = await _get_tenant_tax_config(conn, tenant_id)
                order_ids = [o["id"] for o in orders]
                if order_ids:
                    tax_rows = await conn.fetch(
                        """
                        SELECT
                            COALESCE(p.tax_category, 'standard') AS tax_category,
                            COALESCE(SUM(oi.subtotal), 0) AS subtotal
                        FROM order_items oi
                        JOIN orders o ON o.id = oi.order_id
                        JOIN product p ON p.id = oi.product_id
                        WHERE o.id = ANY($1::uuid[])
                        GROUP BY COALESCE(p.tax_category, 'standard')
                        """,
                        order_ids,
                    )
                    _std_tax, _liq_tax, _tax_label = _compute_tax_breakdown(tax_rows, tax_config)
            except Exception as _e:
                logger.warning(f"Tax breakdown failed for mesa current session (table {table_id}): {_e}")

            # Fetch individual order items for this session (with IDs for edit/delete)
            tab_items = await conn.fetch(
                """
                SELECT
                    oi.id AS order_item_id,
                    oi.quantity,
                    oi.price_at_purchase,
                    oi.subtotal,
                    oi.fulfillment_status,
                    oi.sent_at,
                    p.name AS product_name
                FROM order_items oi
                JOIN orders o ON o.id = oi.order_id
                JOIN product p ON p.id = oi.product_id
                WHERE o.table_session_id = $1
                ORDER BY oi.id ASC
                """,
                session_row["id"],
            )

            # Issue warocol.com#656 — active (non-voided) partial payments of the
            # session, surfaced so checkout can rehydrate "Pagos registrados" on
            # re-entry (otherwise the cashier may double-charge).
            #
            # DISTINCT ON + group_amount collapses mesa proportional splits (one
            # logical payment stored as N rows across orders, same paid_at) into
            # one row per logical payment. The canonical id is the first sibling;
            # the void endpoint already resolves the rest via the
            # (table_session_id, payment_method, paid_at) heuristic.
            #
            # Bar mode filter: only orders still in flight — closed-and-paid
            # orders from previous rounds should not surface.
            partial_payments_rows = await conn.fetch(
                """
                SELECT DISTINCT ON (op.payment_method, op.paid_at)
                    op.id, op.amount, op.payment_method, op.payment_method_id,
                    op.paid_at, pm.name AS payment_method_name,
                    (SELECT COALESCE(SUM(op2.amount), 0)
                       FROM order_payments op2
                       JOIN orders o2 ON o2.id = op2.order_id
                       WHERE o2.table_session_id = $1
                         AND op2.payment_method = op.payment_method
                         AND op2.paid_at = op.paid_at
                         AND op2.voided_at IS NULL) AS group_amount
                FROM order_payments op
                JOIN orders o ON o.id = op.order_id
                LEFT JOIN payment_methods pm ON pm.id = op.payment_method_id
                WHERE o.table_session_id = $1
                  AND op.voided_at IS NULL
                  AND (o.status != 'completed' OR o.payment_status != 'paid')
                ORDER BY op.payment_method, op.paid_at, op.id
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
                    "standard_tax": float(_std_tax),
                    "liquor_tax": float(_liq_tax),
                    "standard_tax_label": _tax_label,
                    # Waiter attribution (warocol.com#574)
                    "attended_by_member_id": str(session_row["attended_by_member_id"]) if session_row.get("attended_by_member_id") else None,
                    "attended_by_member_name": session_row.get("attended_by_member_name"),
                    "attended_by_member_role": session_row.get("attended_by_member_role"),
                    "effective_waiter_member_id": str(session_row["effective_waiter_member_id"]) if session_row.get("effective_waiter_member_id") else None,
                    "effective_waiter_member_name": session_row.get("effective_waiter_member_name"),
                    "effective_waiter_member_role": session_row.get("effective_waiter_member_role"),
                    # Issue warocol.com#656 — rehydration source for checkout's Pagos registrados
                    "partial_payments": [
                        {
                            "id": str(r["id"]),
                            "amount": float(r["group_amount"]),
                            "payment_method": r["payment_method"],
                            "payment_method_id": str(r["payment_method_id"]) if r["payment_method_id"] else None,
                            "payment_method_name": r["payment_method_name"],
                            "paid_at": r["paid_at"].isoformat(),
                        }
                        for r in partial_payments_rows
                    ],
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
                        "id": str(r["order_item_id"]),
                        "productName": r["product_name"],
                        "quantity": r["quantity"],
                        "unitPrice": float(r["price_at_purchase"]),
                        "subtotal": float(r["subtotal"]),
                        "fulfillmentStatus": r["fulfillment_status"],
                        "sentAt": r["sent_at"].isoformat() if r["sent_at"] else None,
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

            # Cancel linked comanda_item BEFORE deleting order_item (FK: no cascade).
            # If comandas are disabled no row will be found — no-op, continues as before.
            comanda_item_row = await conn.fetchrow(
                "SELECT id, comanda_id FROM comanda_items WHERE order_item_id = $1",
                order_item_id,
            )
            if comanda_item_row:
                await conn.execute(
                    """
                    UPDATE comanda_items
                       SET status = 'cancelled', cancelled_at = now()
                     WHERE id = $1
                       AND status NOT IN ('cancelled')
                    """,
                    comanda_item_row["id"],
                )
                await conn.execute(
                    """
                    UPDATE order_items
                       SET fulfillment_status = 'cancelled'
                     WHERE id = $1
                    """,
                    order_item_id,
                )
                # Auto-cancel the parent comanda if every item is now cancelled.
                active_count = await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM comanda_items
                     WHERE comanda_id = $1 AND status != 'cancelled'
                    """,
                    comanda_item_row["comanda_id"],
                )
                if active_count == 0:
                    await conn.execute(
                        """
                        UPDATE comandas
                           SET status = 'cancelled', updated_at = now()
                         WHERE id = $1
                           AND status NOT IN ('delivered', 'cancelled')
                        """,
                        comanda_item_row["comanda_id"],
                    )

                # Nullify FK so order_item can be deleted while the cancelled
                # comanda_item row persists for KDS display (strikethrough in UI).
                await conn.execute(
                    "UPDATE comanda_items SET order_item_id = NULL WHERE id = $1",
                    comanda_item_row["id"],
                )

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

                    # Capture ingredient cost snapshot (needed for COGS GL at close)
                    try:
                        await _capture_order_item_ingredients(
                            conn, order_item_id, item["product_id"],
                            float(item["quantity"]), str(tenant_id)
                        )
                    except Exception as _snap_exc:
                        logger.error(f"[tab] ingredient snapshot failed for item {order_item_id}: {_snap_exc}")

                    # Deduct inventory for each ingredient in the product recipe
                    try:
                        ingredients = await conn.fetch(
                            """
                            SELECT pr.ingredient_id, pr.quantity, pr.unit, i.name as ingredient_name
                            FROM product_recipes pr
                            JOIN ingredients i ON i.id = pr.ingredient_id
                            WHERE pr.product_id = $1
                            UNION ALL
                            -- Issue #517: multiply by pbr.quantity
                            SELECT brt.ingredient_id, brt.base_quantity * pbr.quantity AS quantity, brt.unit, i.name as ingredient_name
                            FROM product_base_recipes pbr
                            JOIN base_recipe_templates brt ON pbr.product_base_type_id = brt.product_base_type_id
                            JOIN ingredients i ON i.id = brt.ingredient_id
                            WHERE pbr.product_id = $1
                            """,
                            item["product_id"],
                        )
                        for ing in ingredients:
                            resolved_qty = await resolve_recipe_quantity_to_base_unit(
                                conn, ing["ingredient_id"],
                                float(ing["quantity"]), ing["unit"] or "",
                            )
                            qty_to_deduct = float(item["quantity"]) * resolved_qty
                            stock_row = await conn.fetchrow(
                                "SELECT current_stock FROM tenant_inventory WHERE ingredient_id = $1 AND tenant_id = $2",
                                ing["ingredient_id"], tenant_id,
                            )
                            if stock_row:
                                prev = float(stock_row["current_stock"] or 0)
                                new_stock = prev - qty_to_deduct
                                await conn.execute(
                                    "UPDATE tenant_inventory SET current_stock = $1, last_updated = NOW() WHERE ingredient_id = $2 AND tenant_id = $3",
                                    new_stock, ing["ingredient_id"], tenant_id,
                                )
                            else:
                                new_stock = -qty_to_deduct
                                await conn.execute(
                                    "INSERT INTO tenant_inventory (tenant_id, ingredient_id, current_stock, minimum_stock, last_updated) VALUES ($1, $2, $3, 0, NOW())",
                                    tenant_id, ing["ingredient_id"], new_stock,
                                )
                            await conn.execute(
                                """
                                INSERT INTO tenant_ingredient_movements (
                                    tenant_id, ingredient_id, movement_type, quantity_change,
                                    unit, previous_stock, new_stock, reference_table, reference_id,
                                    reason, created_by, created_at
                                ) VALUES ($1, $2, 'consumption', $3, $4, $5, $6, 'orders', $7, $8, $9, NOW())
                                """,
                                tenant_id, ing["ingredient_id"], -qty_to_deduct,
                                ing["unit"] or "und",
                                float(stock_row["current_stock"] or 0) if stock_row else 0.0,
                                new_stock, order_id,
                                f"Venta de {item['quantity']}x (mesa {table_id}) - Orden #{order_number}",
                                user_id,
                            )
                    except Exception as _inv_exc:
                        logger.error(f"[tab] inventory deduction failed for item {order_item_id}: {_inv_exc}")

        # Auto-fire comandas if enabled
        try:
            await fire_table_items(request, table_id)
        except Exception as _fe:
            logger.error(f"[add_tab_items] Auto-fire failed for table {table_id}: {_fe}")

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

        # Auto-fire any remaining 'new' items when requesting bill (Issue #419)
        try:
            await fire_table_items(request, table_id)
        except Exception as _fe:
            logger.error(f"[request_bill] Auto-fire failed for table {table_id}: {_fe}")

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
                    "SELECT id, is_bar, name FROM tables WHERE id = $1 AND tenant_id = $2 AND is_active = true FOR UPDATE",
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
                # Cancel comanda_items pointing at the order_items we are about
                # to delete. comanda_items.order_item_id has FK NO ACTION, so a
                # raw DELETE on order_items would raise ForeignKeyViolationError
                # whenever KDS comandas exist. Same pattern as #158 in clear_tab.
                await conn.execute(
                    """
                    UPDATE comanda_items
                    SET status = 'cancelled',
                        cancelled_at = NOW(),
                        order_item_id = NULL
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


async def move_table_session(request: Request, source_table_id: UUID, target_table_id: UUID) -> dict:
    """
    Transfer all pending orders from source table's open session to a new session on target table.
    - Source session is closed; target gets a new session.
    - Completed orders stay on source session for billing history integrity.
    - Returns 400 if source == target.
    - Returns 404 if source/target table not found or source has no open session.
    - Returns 409 if source is a bar table, target is a bar table, or target is occupied.

    Issue: https://github.com/uno0uno/warocol.com/issues/314
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        if source_table_id == target_table_id:
            raise APIError("source and target are the same table", status_code=400)

        async with get_db_connection() as conn:
            async with conn.transaction():
                # 1. Lock + validate source table
                source = await conn.fetchrow(
                    "SELECT id, name, status, is_bar FROM tables "
                    "WHERE id = $1 AND tenant_id = $2 AND is_active = true FOR UPDATE",
                    source_table_id, tenant_id,
                )
                if not source:
                    raise NotFoundError("source table not found")
                if source["is_bar"]:
                    raise APIError("cannot move bar table session", status_code=409)

                # 2. Fetch source open session
                source_session = await conn.fetchrow(
                    "SELECT id FROM table_sessions "
                    "WHERE table_id = $1 AND tenant_id = $2 AND closed_at IS NULL LIMIT 1",
                    source_table_id, tenant_id,
                )
                if not source_session:
                    raise NotFoundError("source table has no open session")

                # 3. Lock + validate target table
                target = await conn.fetchrow(
                    "SELECT id, name, status, is_bar FROM tables "
                    "WHERE id = $1 AND tenant_id = $2 AND is_active = true FOR UPDATE",
                    target_table_id, tenant_id,
                )
                if not target:
                    raise NotFoundError("target table not found")
                if target["is_bar"]:
                    raise APIError("cannot move to bar table", status_code=409)

                # 4. Check target has no open session
                target_open = await conn.fetchrow(
                    "SELECT id FROM table_sessions "
                    "WHERE table_id = $1 AND tenant_id = $2 AND closed_at IS NULL LIMIT 1",
                    target_table_id, tenant_id,
                )
                if target_open:
                    raise APIError("target table is occupied", status_code=409)

                # 5. Create new session on target
                new_session = await conn.fetchrow(
                    "INSERT INTO table_sessions (table_id, tenant_id, opened_by_user_id) "
                    "VALUES ($1, $2, $3) RETURNING id",
                    target_table_id, tenant_id, user_id,
                )
                new_session_id = new_session["id"]

                # 6. Reassign pending orders from source session to new target session
                result = await conn.execute(
                    "UPDATE orders SET table_session_id = $1 "
                    "WHERE table_session_id = $2 AND status = 'pending'",
                    new_session_id, source_session["id"],
                )
                orders_transferred = int(result.split()[-1])

                # 7. Close source session
                await conn.execute(
                    "UPDATE table_sessions SET closed_at = now() WHERE id = $1",
                    source_session["id"],
                )

                # 8. Update table statuses
                await conn.execute(
                    "UPDATE tables SET status = 'free' WHERE id = $1 AND tenant_id = $2",
                    source_table_id, tenant_id,
                )
                await conn.execute(
                    "UPDATE tables SET status = 'open' WHERE id = $1 AND tenant_id = $2",
                    target_table_id, tenant_id,
                )

        logger.info(
            f"[move_table_session] {source_table_id} → {target_table_id}: "
            f"{orders_transferred} orders transferred, new session {new_session_id}"
        )
        return {
            "success": True,
            "data": {
                "source_table_id": str(source_table_id),
                "source_table_name": source["name"],
                "source_session_id": str(source_session["id"]),
                "target_table_id": str(target_table_id),
                "target_table_name": target["name"],
                "target_session_id": str(new_session_id),
                "orders_transferred": orders_transferred,
            },
        }

    except (AuthenticationError, NotFoundError, APIError):
        raise
    except Exception as e:
        logger.error(f"Error moving table session {source_table_id} → {target_table_id}: {e}")
        raise APIError(f"Error moving table session: {e}", status_code=500)


async def set_table_qr_enabled(request: Request, table_id: UUID, enabled: bool) -> dict:
    """Enable/disable Table QR for a table. Generates token on first enable."""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                await _require_table_qr_module(conn, tenant_id)
                table = await _get_table_for_qr_management(conn, tenant_id, table_id)
                token = table["qr_public_token"]
                if enabled:
                    if not token:
                        token = await _generate_unique_qr_token(conn)
                    row = await conn.fetchrow(
                        """
                        UPDATE tables
                        SET qr_enabled = true, qr_public_token = $1
                        WHERE id = $2 AND tenant_id = $3
                        RETURNING id, name, capacity, status, is_active, is_bar,
                                  qr_enabled, qr_public_token, created_at
                        """,
                        token,
                        table_id,
                        tenant_id,
                    )
                else:
                    row = await conn.fetchrow(
                        """
                        UPDATE tables
                        SET qr_enabled = false
                        WHERE id = $1 AND tenant_id = $2
                        RETURNING id, name, capacity, status, is_active, is_bar,
                                  qr_enabled, qr_public_token, created_at
                        """,
                        table_id,
                        tenant_id,
                    )

        return {"success": True, "data": _format_table_simple(row)}

    except (AuthenticationError, NotFoundError, APIError):
        raise
    except Exception as e:
        logger.error(f"Error setting table QR enabled={enabled} for {table_id}: {e}")
        raise APIError(f"Error updating table QR: {e}", status_code=500)


async def regenerate_table_qr_token(request: Request, table_id: UUID) -> dict:
    """Issue a new public token; previous printed QRs stop resolving."""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                await _require_table_qr_module(conn, tenant_id)
                await _get_table_for_qr_management(conn, tenant_id, table_id)
                token = await _generate_unique_qr_token(conn)
                row = await conn.fetchrow(
                    """
                    UPDATE tables
                    SET qr_public_token = $1, qr_enabled = true
                    WHERE id = $2 AND tenant_id = $3
                    RETURNING id, name, capacity, status, is_active, is_bar,
                              qr_enabled, qr_public_token, created_at
                    """,
                    token,
                    table_id,
                    tenant_id,
                )

        return {"success": True, "data": _format_table_simple(row)}

    except (AuthenticationError, NotFoundError, APIError):
        raise
    except Exception as e:
        logger.error(f"Error regenerating QR token for table {table_id}: {e}")
        raise APIError(f"Error regenerating table QR token: {e}", status_code=500)


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
        **_format_table_qr_fields(row),
        "has_history": row.get("last_closed_session_id") is not None,
        "session": None,
        "last_closed_at": row["last_closed_at"].isoformat() if row.get("last_closed_at") else None,
        "last_closed_session_id": str(row["last_closed_session_id"]) if row.get("last_closed_session_id") else None,
        # Default waiter assignment (warocol.com#573) — denormalized for the
        # admin panel + future POS surfaces without an extra round-trip.
        "assigned_member_id": str(row["assigned_member_id"]) if row.get("assigned_member_id") else None,
        "assigned_member_name": row.get("assigned_member_name"),
        "assigned_member_role": row.get("assigned_member_role"),
        # Effective waiter (warocol.com#574) — resolved server-side via COALESCE
        # of session.attended_by > table.assigned_member > NULL.
        "effective_waiter_member_id": str(row["effective_waiter_member_id"]) if row.get("effective_waiter_member_id") else None,
        "effective_waiter_member_name": row.get("effective_waiter_member_name"),
        "effective_waiter_member_role": row.get("effective_waiter_member_role"),
    }
    if row["session_id"]:
        result["session"] = {
            "id": str(row["session_id"]),
            "opened_at": row["opened_at"].isoformat(),
            "duration_minutes": round(float(row["session_duration_minutes"]), 1),
            "running_total": float(row["running_total"]),
            "unfired_count": int(row["unfired_count"]) if row.get("unfired_count") is not None else 0,
            # Session-level waiter override (warocol.com#574)
            "attended_by_member_id": str(row["session_attended_by_member_id"]) if row.get("session_attended_by_member_id") else None,
            "attended_by_member_name": row.get("session_attended_by_member_name"),
            "attended_by_member_role": row.get("session_attended_by_member_role"),
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

                # Cancel comanda_items that point at the order_items we're
                # about to delete. Two reasons:
                #   1. comanda_items.order_item_id has FK to order_items(id)
                #      with NO ACTION → DELETE on order_items raises
                #      ForeignKeyViolationError when KDS comandas exist.
                #   2. Kitchen needs to see status='cancelled' so they know
                #      these items were dropped (not lost).
                # We also NULL-out order_item_id so the FK no longer blocks.
                await conn.execute(
                    """
                    UPDATE comanda_items
                    SET status = 'cancelled',
                        cancelled_at = NOW(),
                        order_item_id = NULL
                    WHERE order_item_id IN (
                        SELECT oi.id
                        FROM order_items oi
                        JOIN orders o ON o.id = oi.order_id
                        WHERE o.table_session_id = $1 AND o.status = 'pending'
                    )
                    """,
                    session_row["id"],
                )

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


async def fire_table_items(request: Request, table_id: UUID) -> dict:
    """
    Explicitly fire all 'new' items in the current table session to the KDS.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # 1. Check if comandas are enabled
            prof = await conn.fetchrow(
                "SELECT comandas_enabled FROM tenant_public_profiles WHERE tenant_id = $1",
                tenant_id
            )
            if not prof or not prof["comandas_enabled"]:
                return {"success": True, "comandas": [], "fired_items_count": 0, "message": "KDS disabled"}

            # 2. Get active session and table name
            table_row = await conn.fetchrow(
                "SELECT id, name FROM tables WHERE id = $1 AND tenant_id = $2 AND is_active = true",
                table_id, tenant_id
            )
            if not table_row:
                raise NotFoundError("Table not found")

            session_row = await conn.fetchrow(
                "SELECT id FROM table_sessions WHERE table_id = $1 AND tenant_id = $2 AND closed_at IS NULL",
                table_id, tenant_id
            )
            if not session_row:
                raise NotFoundError("No open session found")

            # 3. Fire each pending order in this session
            orders = await conn.fetch(
                "SELECT id FROM orders WHERE table_session_id = $1",
                session_row["id"]
            )

            all_comandas = []
            total_fired = 0

            # Use a single connection for all fire calls if possible, or let fire_comandas handle it
            # Since we are already in a connection, we can pass it if we wrap in a transaction
            async with conn.transaction():
                for ord_row in orders:
                    res = await fire_comandas(
                        order_id=ord_row["id"],
                        tenant_id=tenant_id,
                        source_type='table',
                        table_display_name=table_row["name"],
                        conn=conn
                    )
                    all_comandas.extend(res)
                    total_fired += sum(len(c.get('items', [])) for c in res)

        return {
            "success": True,
            "data": {
                "comandas": all_comandas,
                "fired_items_count": total_fired
            }
        }

    except (AuthenticationError, NotFoundError, APIError):
        raise
    except Exception as e:
        logger.error(f"Error firing table {table_id}: {e}")
        raise APIError(f"Error firing table: {e}", status_code=500)


def _format_table_simple(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "capacity": row["capacity"],
        "status": row["status"],
        "is_active": row["is_active"],
        "created_at": row["created_at"].isoformat(),
        **_format_table_qr_fields(row),
    }


async def void_table_payment(
    request: Request,
    table_id: UUID,
    payment_id: UUID,
    reason: Optional[str] = None,
) -> dict:
    """
    Issue warocol.com#649 — soft-delete a mesa partial payment.

    Mesa proportional splits store one logical payment as N rows (one per order
    in the session) at the same paid_at timestamp. The void targets a single
    payment_id but voids the entire sibling group identified by
    (table_session_id, payment_method, paid_at) so the books stay consistent.

    Recomputes the session's paid_total over remaining rows. When voiding flips
    the session out of fully-paid, reopens it (closed_at=NULL, table.status='open')
    and auto-reverses the per-order GL entries inside the same transaction.

    `reason` is optional (audit-only). Empty defaults to "Sin motivo".
    """
    normalized_reason = (reason or '').strip() or 'Sin motivo'

    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id if hasattr(session_context, 'user_id') else None
        role = session_context.role if hasattr(session_context, 'role') else None
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                # 1. Lock target payment + identify its sibling group.
                target = await conn.fetchrow(
                    """
                    SELECT op.id, op.order_id, op.payment_method, op.paid_at,
                           op.cash_received, op.created_by_user_id, op.voided_at,
                           o.table_session_id
                    FROM order_payments op
                    JOIN orders o ON o.id = op.order_id
                    WHERE op.id = $1::uuid AND op.tenant_id = $2
                    FOR UPDATE OF op
                    """,
                    payment_id, tenant_id,
                )
                if not target:
                    raise APIError("Pago no encontrado", status_code=404)
                if target["voided_at"] is not None:
                    raise APIError("Este pago ya fue anulado", status_code=409)
                if target["table_session_id"] is None:
                    raise APIError("El pago no pertenece a una sesión de mesa", status_code=400)

                # 2. Validate the session belongs to the URL's table.
                session_row = await conn.fetchrow(
                    "SELECT id, table_id, closed_at FROM table_sessions WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
                    target["table_session_id"], tenant_id,
                )
                if not session_row or str(session_row["table_id"]) != str(table_id):
                    raise APIError("La sesión no corresponde a esta mesa", status_code=400)

                # 3. Authorization: creator OR manager-level role.
                creator_id = target["created_by_user_id"]
                if creator_id is not None and str(creator_id) != str(user_id) and role not in _PAYMENT_VOID_ROLES:
                    raise APIError(
                        "Solo el cajero que registró el pago o un administrador puede anularlo",
                        status_code=403,
                    )

                # 4. Find sibling rows (proportional split): same session,
                # same method, same paid_at, not voided. Lock them all.
                sibling_rows = await conn.fetch(
                    """
                    SELECT op.id, op.order_id, op.amount
                    FROM order_payments op
                    JOIN orders o ON o.id = op.order_id
                    WHERE o.table_session_id = $1
                      AND op.payment_method = $2
                      AND op.paid_at = $3
                      AND op.voided_at IS NULL
                      AND op.tenant_id = $4
                    FOR UPDATE OF op
                    """,
                    session_row["id"], target["payment_method"], target["paid_at"], tenant_id,
                )
                if not sibling_rows:
                    raise APIError("Pago no encontrado en la sesión", status_code=404)

                voided_ids = [str(r["id"]) for r in sibling_rows]
                affected_order_ids = list({r["order_id"] for r in sibling_rows})

                # 5. Soft-delete all siblings atomically.
                await conn.execute(
                    "UPDATE order_payments SET voided_at = NOW() WHERE id = ANY($1::uuid[])",
                    [r["id"] for r in sibling_rows],
                )

                # 6. Recompute session-wide paid_total / remaining.
                session_orders = await conn.fetch(
                    "SELECT id, total_amount FROM orders WHERE table_session_id = $1",
                    session_row["id"],
                )
                session_total = sum(float(r["total_amount"]) for r in session_orders)
                order_ids = [r["id"] for r in session_orders]
                paid_row = await conn.fetchrow(
                    "SELECT COALESCE(SUM(amount), 0) AS paid FROM order_payments WHERE order_id = ANY($1) AND voided_at IS NULL",
                    order_ids,
                )
                paid_total = float(paid_row["paid"])
                remaining = max(0.0, session_total - paid_total)
                is_complete = remaining <= 0.01

                # 7. Reopen if voiding flipped the session out of fully-paid.
                was_closed = session_row["closed_at"] is not None
                reopened = was_closed and not is_complete
                if reopened:
                    await conn.execute(
                        "UPDATE orders SET payment_status = 'partial' WHERE table_session_id = $1 AND status = 'completed' AND payment_status = 'paid'",
                        session_row["id"],
                    )
                    await conn.execute(
                        "UPDATE table_sessions SET closed_at = NULL WHERE id = $1",
                        session_row["id"],
                    )
                    await conn.execute(
                        "UPDATE tables SET status = 'open' WHERE id = $1 AND tenant_id = $2",
                        table_id, tenant_id,
                    )
                    # Reverse posted GL entry per affected order.
                    for affected_order_id in affected_order_ids:
                        await void_order_journal_entry_in_txn(
                            conn, tenant_id, affected_order_id, user_id, normalized_reason,
                        )

        logger.info(
            f"Mesa payments voided: session={session_row['id']} group_size={len(voided_ids)} paid_total={paid_total} reopened={reopened}"
        )
        return {
            "success": True,
            "data": {
                "voided_ids": voided_ids,
                "paid_total": paid_total,
                "remaining": remaining,
                "is_complete": is_complete,
                "reopened": reopened,
            },
        }

    except (AuthenticationError, APIError):
        raise
    except Exception as e:
        logger.error(f"Error voiding mesa payment {payment_id}: {e}")
        raise APIError(f"Error al anular el pago: {e}", status_code=500)

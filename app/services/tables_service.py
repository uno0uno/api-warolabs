"""
Tables Service
Business logic for table management and session lifecycle.

Issue: https://github.com/uno0uno/warocol.com/issues/298
"""
import json
import secrets
from typing import Optional, List, Any, Dict
from uuid import UUID
from datetime import date
from decimal import Decimal
from fastapi import Request
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError, APIError, NotFoundError
from app.core.timezones import local_date_for_tenant, resolve_tenant_timezone
from app.services.cierre_service import (
    _get_tenant_tax_config,
    _post_order_gl_entry,
    _post_order_cogs_gl_entry,
    _post_deferred_order_tip_gl,
)
from app.services.accounting_service import void_order_journal_entry_in_txn
from app.services.pos_cart_service import (
    _capture_order_item_ingredients,
    _deduct_modifier_inventory_for_order_item,
    _order_payment_splits_for_gl,
    _PAYMENT_VOID_ROLES,
    _tax_rows_from_evaluated_lines,
    add_order_payment,
    void_order_payment,
)
from app.services.orders_service import (
    _compute_tax_breakdown,
    _deduct_stock_for_status_update,
    _order_inventory_already_consumed_before_completion,
    _return_ingredient_to_stock,
    _return_stock_for_order_cancellation,
    get_order_by_id,
    get_order_items,
    update_order_status,
)
from app.utils.table_code import infer_table_code, normalize_table_code, resolve_unique_code
from app.services.table_session_guests import guest_snapshot_from_capacity
from app.services.tip_tax_service import (
    compute_tip_tax_amount,
    normalize_tip_payload,
    split_settlement_amount_due,
    tip_settlement_total,
)
from app.services.ingredient_purchase_units_service import resolve_recipe_quantity_to_base_unit
from app.services.comandas_service import _parse_item_row, fire_comandas
from app.services.billing_service import check_plan_quota_growth
from app.services.operation_events_service import DOMAIN_POS, record_operation_event
from app.services.open_priced_service import (
    fetch_product_pricing_map,
    validate_items_unit_prices,
)
from app.services.modifier_option_service import (
    modifier_line_subtotal,
    resolve_modifier_selections,
)
from app.services.table_session_advances_service import (
    TABLE_SESSION_ADVANCE_PAYMENT_SLUG,
    apply_session_advances_for_close,
    get_available_advance_total,
    recognize_unconsumed_advance_cover_for_close,
)
import logging

logger = logging.getLogger(__name__)

_QR_TOKEN_URLSAFE_BYTES = 32
_QR_TOKEN_MAX_ATTEMPTS = 5


def _completed_session_orders_payload(order_rows: List[Any]) -> Dict[str, Any]:
    """Return invoice-friendly order identifiers for a completed table session."""
    order_ids = [str(row["id"]) for row in order_rows]
    order_numbers = [
        int(row["order_number"]) if row["order_number"] is not None else 0
        for row in order_rows
    ]
    payload: Dict[str, Any] = {
        "order_ids": order_ids,
        "order_numbers": order_numbers,
        "status": "completed",
        "payment_status": "paid",
        "total_amount": float(sum(float(row["total_amount"] or 0) for row in order_rows)),
    }
    if len(order_ids) == 1:
        payload["order_id"] = order_ids[0]
        payload["order_number"] = order_numbers[0]
    return payload


async def _recalc_order_total_from_items(conn, order_id: UUID) -> None:
    """Recompute orders.total_amount from line net/subtotal after a merge."""
    await conn.execute(
        """
        UPDATE orders
        SET total_amount = COALESCE((
            SELECT SUM(COALESCE(oi.net_total, oi.subtotal))
            FROM order_items oi
            WHERE oi.order_id = $1
        ), 0)
        WHERE id = $1
        """,
        order_id,
    )


async def _merge_order_into_primary(
    conn,
    primary_order_id: UUID,
    secondary_order_id: UUID,
) -> None:
    """Move checkout lines and payments onto the primary order, then drop the shell."""
    if primary_order_id == secondary_order_id:
        return

    collisions = await conn.fetch(
        """
        SELECT sec.id AS secondary_item_id,
               pri.id AS primary_item_id,
               sec.quantity AS sec_qty,
               sec.subtotal AS sec_subtotal,
               COALESCE(sec.net_total, sec.subtotal) AS sec_net,
               COALESCE(sec.discount_allocated, 0) AS sec_discount
        FROM order_items sec
        JOIN order_items pri
          ON pri.order_id = $1
         AND sec.order_id = $2
         AND sec.variant_id IS NOT NULL
         AND pri.variant_id = sec.variant_id
        """,
        primary_order_id,
        secondary_order_id,
    )
    for row in collisions:
        await conn.execute(
            """
            UPDATE order_items
            SET quantity = quantity + $2,
                subtotal = subtotal + $3,
                net_total = COALESCE(net_total, subtotal) + $4,
                discount_allocated = COALESCE(discount_allocated, 0) + $5,
                updated_at = now()
            WHERE id = $1
            """,
            row["primary_item_id"],
            row["sec_qty"],
            row["sec_subtotal"],
            row["sec_net"],
            row["sec_discount"],
        )
        await conn.execute(
            "UPDATE comanda_items SET order_item_id = $1 WHERE order_item_id = $2",
            row["primary_item_id"],
            row["secondary_item_id"],
        )
        await conn.execute("DELETE FROM order_items WHERE id = $1", row["secondary_item_id"])

    await conn.execute(
        "UPDATE order_items SET order_id = $1, updated_at = now() WHERE order_id = $2",
        primary_order_id,
        secondary_order_id,
    )
    await conn.execute(
        "UPDATE order_payments SET order_id = $1 WHERE order_id = $2",
        primary_order_id,
        secondary_order_id,
    )
    await conn.execute(
        "UPDATE comandas SET order_id = $1, updated_at = now() WHERE order_id = $2",
        primary_order_id,
        secondary_order_id,
    )
    await conn.execute("DELETE FROM orders WHERE id = $1", secondary_order_id)


async def _merge_duplicate_pending_orders_for_session(conn, session_id: UUID) -> None:
    pending_rows = await conn.fetch(
        """
        SELECT id FROM orders
        WHERE table_session_id = $1 AND status = 'pending'
        ORDER BY created_at, id
        """,
        session_id,
    )
    if len(pending_rows) <= 1:
        return
    primary_id = pending_rows[0]["id"]
    for row in pending_rows[1:]:
        await _merge_order_into_primary(conn, primary_id, row["id"])
    await _recalc_order_total_from_items(conn, primary_id)


async def _fold_pending_orders_into_completed_for_session(conn, session_id: UUID) -> None:
    pending_rows = await conn.fetch(
        """
        SELECT id FROM orders
        WHERE table_session_id = $1 AND status = 'pending'
        ORDER BY created_at, id
        """,
        session_id,
    )
    completed_rows = await conn.fetch(
        """
        SELECT id FROM orders
        WHERE table_session_id = $1 AND status = 'completed'
        ORDER BY created_at, id
        """,
        session_id,
    )
    if not pending_rows or not completed_rows:
        return
    primary_id = completed_rows[0]["id"]
    for row in pending_rows:
        await _merge_order_into_primary(conn, primary_id, row["id"])
    await _recalc_order_total_from_items(conn, primary_id)


async def _merge_duplicate_completed_orders_for_session(conn, session_id: UUID) -> None:
    completed_rows = await conn.fetch(
        """
        SELECT id FROM orders
        WHERE table_session_id = $1 AND status = 'completed'
        ORDER BY created_at, id
        """,
        session_id,
    )
    if len(completed_rows) <= 1:
        return
    primary_id = completed_rows[0]["id"]
    for row in completed_rows[1:]:
        await _merge_order_into_primary(conn, primary_id, row["id"])
    await _recalc_order_total_from_items(conn, primary_id)


async def _consolidate_session_orders_for_checkout(
    conn,
    session_id: UUID,
    *,
    fold_pending_into_completed: bool = True,
) -> None:
    """
    Ensure a table session checkout produces a single order where possible.

    - Multiple pending orders → oldest pending absorbs the rest.
    - Pending + completed/partial → pending lines fold into oldest completed (optional).
    - Multiple completed/partial → oldest completed absorbs siblings (split legacy).

    close_session defers fold_pending_into_completed until after pending settlement.
    """
    await _merge_duplicate_pending_orders_for_session(conn, session_id)
    if fold_pending_into_completed:
        await _fold_pending_orders_into_completed_for_session(conn, session_id)
    await _merge_duplicate_completed_orders_for_session(conn, session_id)


def _modifier_unit_total(mod: dict) -> float:
    return float(
        modifier_line_subtotal(
            mod.get("price", 0),
            mod.get("quantity", 1),
            mod.get("included_quantity", 0),
        )
    )


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


async def _get_minimum_consumption_snapshot(conn, tenant_id) -> Dict[str, Any]:
    row = await conn.fetchrow(
        """
        SELECT
            minimum_consumption_enabled,
            minimum_consumption_amount,
            minimum_consumption_restrictive
        FROM tenant_public_profiles
        WHERE tenant_id = $1
        """,
        tenant_id,
    )
    if not row:
        return {
            "enabled": False,
            "amount": Decimal("0"),
            "restrictive": False,
        }
    amount = row["minimum_consumption_amount"] or Decimal("0")
    return {
        "enabled": bool(row["minimum_consumption_enabled"]) and amount > 0,
        "amount": amount,
        "restrictive": bool(row["minimum_consumption_restrictive"]),
    }


def _minimum_consumption_state(
    row: dict,
    paid_amount: float = 0.0,
    advance_amount: float = 0.0,
) -> dict:
    enabled = bool(row.get("minimum_consumption_enabled_snapshot"))
    amount = float(row.get("minimum_consumption_amount_snapshot") or 0)
    consumed = float(row.get("running_total") or 0)
    paid = float(paid_amount or 0)
    advance = float(advance_amount or 0)
    covered_amount = consumed + paid + advance
    remaining = max(amount - covered_amount, 0) if enabled else 0
    return {
        "enabled": enabled,
        "amount": amount,
        "restrictive": bool(row.get("minimum_consumption_restrictive_snapshot")),
        "consumed": consumed,
        "paid": paid,
        "advance": advance,
        "advance_total": advance,
        "covered_amount": covered_amount,
        "remaining": remaining,
        "missing": remaining,
        "overage_due": max(consumed - paid - advance, 0),
        "covered": (not enabled) or remaining <= 0,
    }


def _minimum_consumption_close_state(
    session_row: dict,
    consumed_total: Decimal,
    advance_total: Decimal,
) -> dict:
    state = _minimum_consumption_state(
        {
            **dict(session_row),
            "running_total": consumed_total,
        },
        advance_amount=float(advance_total),
    )
    state["apply_amount"] = float(min(consumed_total, advance_total))
    return state


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


async def _tenant_table_codes(conn, tenant_id, exclude_table_id=None) -> set[str]:
    rows = await conn.fetch(
        """
        SELECT code FROM tables
        WHERE tenant_id = $1
          AND code IS NOT NULL
          AND deleted_at IS NULL
          AND ($2::uuid IS NULL OR id != $2)
        """,
        tenant_id,
        exclude_table_id,
    )
    return {str(r["code"]).upper() for r in rows}


async def _resolve_table_code(
    conn,
    tenant_id,
    name: str,
    *,
    is_bar: bool = False,
    explicit_code: Optional[str] = None,
    exclude_table_id=None,
) -> str:
    if is_bar:
        return "BAR"

    used = await _tenant_table_codes(conn, tenant_id, exclude_table_id)

    if explicit_code is not None and str(explicit_code).strip():
        normalized = normalize_table_code(explicit_code)
        if normalized and normalized.upper() in used:
            raise APIError(f"Table code '{normalized}' is already in use", status_code=409)
        return normalized or infer_table_code(name)

    proposed = infer_table_code(name)
    return resolve_unique_code(proposed, used)


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
            INSERT INTO tables (tenant_id, name, capacity, status, is_bar, code)
            VALUES ($1, 'Barra', NULL, 'open', TRUE, 'BAR')
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
        minimum_snapshot = await _get_minimum_consumption_snapshot(conn, tenant_id)
        await conn.execute(
            """
            INSERT INTO table_sessions (
                table_id,
                tenant_id,
                opened_by_user_id,
                minimum_consumption_enabled_snapshot,
                minimum_consumption_amount_snapshot,
                minimum_consumption_restrictive_snapshot
            )
            VALUES ($1, $2, NULL, $3, $4, $5)
            """,
            bar_table_id,
            tenant_id,
            minimum_snapshot["enabled"],
            minimum_snapshot["amount"],
            minimum_snapshot["restrictive"],
        )

    # Ensure bar table status is 'open'
    if bar_row["status"] != "open":
        await conn.execute(
            "UPDATE tables SET status = 'open' WHERE id = $1 AND tenant_id = $2",
            bar_table_id,
            tenant_id,
        )


async def _next_regular_table_display_order(conn, tenant_id) -> int:
    value = await conn.fetchval(
        """
        SELECT COALESCE(MAX(display_order), 0) + 1
        FROM tables
        WHERE tenant_id = $1
          AND deleted_at IS NULL
          AND is_bar IS FALSE
        """,
        tenant_id,
    )
    return int(value or 1)


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
                    t.code,
                    t.capacity,
                    t.status,
                    t.is_active,
                    t.is_bar,
                    t.qr_enabled,
                    t.qr_public_token,
                    t.display_order,
                    t.created_at,
                    t.assigned_member_id,
                    p_assigned.name AS assigned_member_name,
                    tm_assigned.role AS assigned_member_role,
                    ts.id            AS session_id,
                    ts.opened_at,
                    ts.opened_by_user_id,
                    ts.attended_by_member_id AS session_attended_by_member_id,
                    ts.custom_label AS session_custom_label,
                    ts.covers AS session_covers,
                    ts.capacity_snapshot AS session_capacity_snapshot,
                    ts.minimum_consumption_enabled_snapshot,
                    ts.minimum_consumption_amount_snapshot,
                    ts.minimum_consumption_restrictive_snapshot,
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
                        (SELECT SUM(op.amount)
                         FROM order_payments op
                         JOIN orders op_o ON op_o.id = op.order_id
                         WHERE op_o.table_session_id = ts.id
                           AND op.voided_at IS NULL),
                        0
                    ) AS paid_total,
                    COALESCE(
                        (SELECT SUM(tsa.amount_cop - COALESCE(tsa.applied_amount_cop, 0))
                         FROM table_session_advances tsa
                         WHERE tsa.table_session_id = ts.id
                           AND tsa.tenant_id = $1
                           AND tsa.status = 'active'),
                        0
                    ) AS active_advance_total_cop,
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
                ORDER BY t.is_bar DESC, t.is_active DESC, t.display_order NULLS LAST, t.name
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
    code: Optional[str] = None,
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
                try:
                    resolved_code = await _resolve_table_code(
                        conn, tenant_id, name, explicit_code=code,
                    )
                except ValueError as exc:
                    raise APIError(str(exc), status_code=400) from exc

                await check_plan_quota_growth(conn, tenant_id, "active_tables_including_bar")
                display_order = await _next_regular_table_display_order(conn, tenant_id)
                row = await conn.fetchrow(
                    """
                    INSERT INTO tables (tenant_id, name, capacity, code, display_order)
                    VALUES ($1, $2, $3, $4, $5)
                    RETURNING id, name, code, capacity, status, is_active, is_bar,
                              qr_enabled, qr_public_token, display_order, created_at
                    """,
                    tenant_id,
                    name,
                    capacity,
                    resolved_code,
                    display_order,
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
                        RETURNING id, name, code, capacity, status, is_active, is_bar,
                                  qr_enabled, qr_public_token, display_order, created_at
                        """,
                        token,
                        row["id"],
                        tenant_id,
                    )

        logger.info(f"Table created: {row['id']} ({name}, code={row['code']}) for tenant {tenant_id}")
        return {"success": True, "data": _format_table_simple(row)}

    except (AuthenticationError, APIError):
        raise
    except Exception as e:
        logger.error(f"Error creating table: {e}")
        raise APIError(f"Error creating table: {e}", status_code=500)


async def reorder_tables(request: Request, table_ids: List[UUID]) -> dict:
    """
    Persist manual display order for regular tables.
    Submitted ids become the ordered set; omitted regular tables fall back by name.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        if not table_ids:
            raise APIError("table_ids is required", status_code=400)

        unique_ids = list(dict.fromkeys(table_ids))
        if len(unique_ids) != len(table_ids):
            raise APIError("table_ids contains duplicates", status_code=400)

        async with get_db_connection() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    """
                    SELECT id, is_bar, deleted_at
                    FROM tables
                    WHERE tenant_id = $1
                      AND id = ANY($2::uuid[])
                    """,
                    tenant_id,
                    unique_ids,
                )

                rows_by_id = {row["id"]: row for row in rows}
                missing_ids = [table_id for table_id in unique_ids if table_id not in rows_by_id]
                if missing_ids:
                    raise NotFoundError("One or more tables were not found")

                for row in rows:
                    if row["deleted_at"] is not None:
                        raise NotFoundError("One or more tables were not found")
                    if row["is_bar"]:
                        raise APIError("La Barra no puede reordenarse", status_code=409)

                await conn.execute(
                    """
                    UPDATE tables
                    SET display_order = NULL
                    WHERE tenant_id = $1
                      AND deleted_at IS NULL
                      AND is_bar IS FALSE
                    """,
                    tenant_id,
                )
                await conn.execute(
                    """
                    WITH ordered AS (
                        SELECT id, ord::integer AS display_order
                        FROM UNNEST($2::uuid[]) WITH ORDINALITY AS u(id, ord)
                    )
                    UPDATE tables t
                    SET display_order = ordered.display_order
                    FROM ordered
                    WHERE t.tenant_id = $1
                      AND t.id = ordered.id
                      AND t.deleted_at IS NULL
                      AND t.is_bar IS FALSE
                    """,
                    tenant_id,
                    unique_ids,
                )

        return {
            "success": True,
            "message": "Orden de mesas actualizado",
            "data": {
                "table_ids": [str(table_id) for table_id in unique_ids],
            },
        }

    except (AuthenticationError, NotFoundError, APIError):
        raise
    except Exception as e:
        logger.error(f"Error reordering tables: {e}")
        raise APIError(f"Error reordering tables: {e}", status_code=500)


async def update_table(
    request: Request,
    table_id: UUID,
    updates: dict,
) -> dict:
    """
    Update a table's name, code, and/or capacity (status is NOT editable here).
    Only keys present in ``updates`` are applied (see router model_dump exclude_unset).
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            existing = await conn.fetchrow(
                """
                SELECT id, name, is_bar, code
                FROM tables
                WHERE id = $1 AND tenant_id = $2 AND is_active = true AND deleted_at IS NULL
                """,
                table_id,
                tenant_id,
            )
            if not existing:
                raise NotFoundError("Table not found")

            if not updates:
                row = await conn.fetchrow(
                    """
                    SELECT id, name, code, capacity, status, is_active, is_bar,
                           qr_enabled, qr_public_token, display_order, created_at
                    FROM tables WHERE id = $1 AND tenant_id = $2
                    """,
                    table_id,
                    tenant_id,
                )
                return {"success": True, "data": _format_table_simple(row)}

            set_clauses: List[str] = []
            params: List[Any] = [table_id, tenant_id]
            idx = 3
            effective_name = updates.get("name", existing["name"])

            if "name" in updates:
                set_clauses.append(f"name = ${idx}")
                params.append(updates["name"])
                idx += 1

            if "capacity" in updates:
                set_clauses.append(f"capacity = ${idx}")
                params.append(updates["capacity"])
                idx += 1

            if "code" in updates:
                if existing["is_bar"]:
                    raise APIError("Bar table code cannot be changed", status_code=400)
                raw_code = updates["code"]
                try:
                    if raw_code is None or (isinstance(raw_code, str) and not raw_code.strip()):
                        resolved_code = await _resolve_table_code(
                            conn,
                            tenant_id,
                            effective_name,
                            explicit_code=None,
                            exclude_table_id=table_id,
                        )
                    else:
                        normalized = normalize_table_code(raw_code)
                        used = await _tenant_table_codes(conn, tenant_id, exclude_table_id=table_id)
                        if normalized and normalized.upper() in used:
                            raise APIError(
                                f"Table code '{normalized}' is already in use",
                                status_code=409,
                            )
                        resolved_code = normalized or await _resolve_table_code(
                            conn,
                            tenant_id,
                            effective_name,
                            explicit_code=None,
                            exclude_table_id=table_id,
                        )
                except ValueError as exc:
                    raise APIError(str(exc), status_code=400) from exc

                set_clauses.append(f"code = ${idx}")
                params.append(resolved_code)
                idx += 1

            row = await conn.fetchrow(
                f"""
                UPDATE tables
                SET {", ".join(set_clauses)}
                WHERE id = $1 AND tenant_id = $2
                RETURNING id, name, code, capacity, status, is_active, is_bar,
                          qr_enabled, qr_public_token, display_order, created_at
                """,
                *params,
            )

        return {"success": True, "data": _format_table_simple(row)}

    except (AuthenticationError, NotFoundError, APIError):
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

            await check_plan_quota_growth(conn, tenant_id, "active_tables_including_bar")
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
                    "SELECT id, status, capacity FROM tables WHERE id = $1 AND tenant_id = $2 AND is_active = true FOR UPDATE",
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

                minimum_snapshot = await _get_minimum_consumption_snapshot(conn, tenant_id)
                covers, capacity_snapshot = guest_snapshot_from_capacity(table_row["capacity"])

                # Create session
                session_row = await conn.fetchrow(
                    """
                    INSERT INTO table_sessions (
                        table_id,
                        tenant_id,
                        opened_by_user_id,
                        attended_by_member_id,
                        minimum_consumption_enabled_snapshot,
                        minimum_consumption_amount_snapshot,
                        minimum_consumption_restrictive_snapshot,
                        covers,
                        capacity_snapshot
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    RETURNING id, opened_at
                    """,
                    table_id,
                    tenant_id,
                    user_id,
                    resolved_attended_by,
                    minimum_snapshot["enabled"],
                    minimum_snapshot["amount"],
                    minimum_snapshot["restrictive"],
                    covers,
                    capacity_snapshot,
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


async def close_session(request: Request, table_id: UUID, payment_method: Optional[str] = None, customer_id: Optional[str] = None, credit_due_date: Optional[date] = None, payment_method_id: Optional[UUID] = None, discount_type: Optional[str] = None, discount_value: Optional[float] = None, split_mode: bool = False, split_first_amount: float = 0.0, *, split_first_cash_received: Optional[float] = None, cash_received: Optional[float] = None, tip_amount: float = 0, tip_source: str = 'none', tip_taxable: bool = False, served_by_member_id: Optional[UUID] = None, reason: Optional[str] = None, waros_to_redeem: Optional[int] = None, waro_reward_id: Optional[UUID] = None) -> dict:
    """
    Close the active session for a table.
    If payment_method is provided, marks all pending orders as completed with that payment method.
    """
    # warocol.com#639 — tip validation (same rules as pos_cart.complete_pos_order)
    try:
        tip_amount, tip_source, tip_taxable = normalize_tip_payload(
            tip_amount, tip_source, tip_taxable,
        )
    except ValueError as exc:
        raise APIError(str(exc), status_code=400)
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
        user_id = session_context.user_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            timezone_name = await resolve_tenant_timezone(conn, tenant_id)
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
                        ts.minimum_consumption_enabled_snapshot,
                        ts.minimum_consumption_amount_snapshot,
                        ts.minimum_consumption_restrictive_snapshot,
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
                _mesa_tip_taxable = bool(tip_taxable) if tip_amount > 0 else False
                _mesa_tip_tax_amount = 0.0
                _promo_savings = 0.0
                _promo_breakdown: List[dict] = []
                _minimum_close_state: Optional[dict] = None
                _advance_apply_amount = Decimal("0")
                _advance_applied_total = Decimal("0")
                _advance_tip_applied_total = Decimal("0")
                _advance_cover_total = Decimal("0")

                available_advance_total = await get_available_advance_total(
                    conn,
                    UUID(str(tenant_id)),
                    session_row["id"],
                )
                if not payment_method and available_advance_total > 0:
                    payment_method = TABLE_SESSION_ADVANCE_PAYMENT_SLUG

                # Mark pending orders as completed if payment_method provided
                if payment_method:
                    await _consolidate_session_orders_for_checkout(
                        conn,
                        session_row["id"],
                        fold_pending_into_completed=False,
                    )
                    # Backend guard: credit / wallet require an identified (non-anonymous) customer
                    if payment_method in ('credit', 'customer_wallet') and customer_id:
                        cust_row = await conn.fetchrow(
                            "SELECT phone_number FROM profile WHERE id = $1::uuid",
                            customer_id
                        )
                        if cust_row and cust_row['phone_number'] == '0000000000':
                            raise APIError(
                                "El pago a crédito o billetera requiere un cliente identificado (no anónimo)",
                                status_code=400
                            )
                    if payment_method == 'customer_wallet' and not customer_id:
                        raise APIError(
                            "La billetera requiere un cliente en la mesa",
                            status_code=400,
                        )
                    if payment_method == TABLE_SESSION_ADVANCE_PAYMENT_SLUG and split_mode:
                        raise APIError(
                            "El anticipo de mesa solo se aplica en el cierre final",
                            status_code=400,
                        )

                    payment_status = 'credit' if payment_method == 'credit' else ('partial' if split_mode else 'paid')

                    from app.services.promotions_service import (
                        enrich_order_item_rows_with_promo_basis,
                        evaluate_checkout_promotions,
                        item_rows_to_promo_lines,
                    )

                    _discount_amount = None
                    _promo_breakdown = []
                    item_rows = await conn.fetch(
                        """
                        SELECT oi.id, oi.subtotal, oi.quantity, oi.price_at_purchase,
                               oi.product_id, oi.promo_opt_out,
                               oi.applied_promotion_id AS locked_promotion_id,
                               tp.name AS locked_promotion_name,
                               tp.promo_type AS locked_promo_type,
                               oi.promo_savings_allocated AS locked_promo_savings,
                               p.category_id,
                               COALESCE(p.tax_category, 'standard') AS tax_category
                        FROM order_items oi
                        JOIN orders o ON o.id = oi.order_id
                        JOIN product p ON p.id = oi.product_id
                        LEFT JOIN tenant_promotions tp ON tp.id = oi.applied_promotion_id
                        WHERE o.table_session_id = $1 AND o.status = 'pending'
                        """,
                        session_row["id"],
                    )
                    item_rows = await enrich_order_item_rows_with_promo_basis(conn, item_rows)
                    promo_lines = item_rows_to_promo_lines(item_rows)
                    checkout_eval = await evaluate_checkout_promotions(
                        conn,
                        UUID(str(tenant_id)),
                        promo_lines,
                        discount_type=discount_type,
                        discount_value=discount_value,
                        preserve_persisted_promos=True,
                    )
                    from app.services.waros_service import (
                        apply_checkout_waro_redemption,
                        settle_waro_redemption,
                    )
                    from fastapi import HTTPException as FastAPIHTTPException

                    _mesa_customer_uuid: Optional[UUID] = None
                    if customer_id:
                        try:
                            _mesa_customer_uuid = UUID(str(customer_id))
                        except ValueError:
                            raise APIError("customer_id no es un UUID válido", status_code=422)

                    try:
                        checkout_eval = await apply_checkout_waro_redemption(
                            conn,
                            tenant_id,
                            _mesa_customer_uuid,
                            checkout_eval,
                            waros_to_redeem=waros_to_redeem,
                            waro_reward_id=waro_reward_id,
                        )
                    except FastAPIHTTPException as waro_exc:
                        raise APIError(waro_exc.detail, status_code=waro_exc.status_code)

                    _waro_preview = checkout_eval.pop("_waro_redemption_preview", None)

                    _promo_savings = float(checkout_eval.get("promo_savings") or 0)
                    _promo_breakdown = checkout_eval.get("promo_breakdown") or []
                    _discount_amount = checkout_eval.get("manual_discount_amount") or None
                    from app.services.promotions_service import (
                        apply_promo_eval_to_order_items,
                        recalc_pending_session_order_totals,
                    )

                    await apply_promo_eval_to_order_items(conn, item_rows, checkout_eval)
                    await recalc_pending_session_order_totals(conn, session_row["id"])

                    if _waro_preview and _mesa_customer_uuid:
                        _first_pending_order = await conn.fetchval(
                            """
                            SELECT id FROM orders
                            WHERE table_session_id = $1 AND status = 'pending'
                            ORDER BY created_at LIMIT 1
                            """,
                            session_row["id"],
                        )
                        if _first_pending_order:
                            try:
                                await settle_waro_redemption(
                                    conn,
                                    tenant_id,
                                    _mesa_customer_uuid,
                                    _first_pending_order,
                                    _waro_preview,
                                )
                            except FastAPIHTTPException as waro_exc:
                                raise APIError(waro_exc.detail, status_code=waro_exc.status_code)
                            await recalc_pending_session_order_totals(conn, session_row["id"])

                    consumed_total = Decimal(str(await conn.fetchval(
                        """
                        SELECT COALESCE(SUM(total_amount), 0)
                        FROM orders
                        WHERE table_session_id = $1
                          AND status = 'pending'
                        """,
                        session_row["id"],
                    ) or 0)).quantize(Decimal("0.01"))
                    available_advance_total = await get_available_advance_total(
                        conn,
                        UUID(str(tenant_id)),
                        session_row["id"],
                    )
                    _minimum_close_state = _minimum_consumption_close_state(
                        session_row,
                        consumed_total,
                        available_advance_total,
                    )
                    if tip_amount > 0:
                        tax_config = await _get_tenant_tax_config(conn, tenant_id)
                        _mesa_tip_tax_amount = compute_tip_tax_amount(
                            float(tip_amount), bool(tip_taxable), tax_config,
                        )
                    _settlement_due_after_advance = max(
                        Decimal("0"),
                        consumed_total
                        + Decimal(str(tip_settlement_total(float(tip_amount), _mesa_tip_tax_amount)))
                        - available_advance_total,
                    )
                    if (
                        not split_mode
                        and _minimum_close_state["enabled"]
                        and _minimum_close_state["restrictive"]
                        and _minimum_close_state["missing"] > 0
                    ):
                        missing = _minimum_close_state["missing"]
                        raise APIError(
                            f"Faltan ${round(missing):,} para cubrir el consumo mínimo",
                            status_code=409,
                            details={
                                "code": "minimum_consumption_not_covered",
                                "minimum_amount": _minimum_close_state["amount"],
                                "consumed": _minimum_close_state["consumed"],
                                "advance_total": _minimum_close_state["advance_total"],
                                "missing": missing,
                            },
                        )
                    if not split_mode and available_advance_total > 0:
                        _advance_apply_amount = Decimal(str(_minimum_close_state["apply_amount"]))
                        if _settlement_due_after_advance <= Decimal("0.01"):
                            payment_method = TABLE_SESSION_ADVANCE_PAYMENT_SLUG
                            payment_method_id = None
                            cash_received = None
                        elif payment_method == TABLE_SESSION_ADVANCE_PAYMENT_SLUG:
                            raise APIError(
                                "Selecciona un método de pago para cobrar el saldo pendiente",
                                status_code=400,
                                details={
                                    "code": "minimum_consumption_overage_payment_required",
                                    "overage_due": float(_settlement_due_after_advance),
                                },
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
                                order_date = now()
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
                                payment_method_id = $6,
                                order_date = now()
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

                    await _merge_duplicate_completed_orders_for_session(conn, session_row["id"])

                    # warocol.com#2566 — if send skipped stock (flag off), deduct at mesa close
                    # before COGS so snapshots + kardex match checkout-time behavior.
                    just_completed = await conn.fetch(
                        """
                        SELECT id, order_number, table_session_id, pos_cart_id
                        FROM orders
                        WHERE table_session_id = $1 AND status = 'completed'
                        """,
                        session_row["id"],
                    )
                    for ord_row in just_completed:
                        try:
                            await _ensure_tab_order_inventory_at_close(
                                conn,
                                tenant_id=tenant_id,
                                user_id=user_id,
                                order_row=ord_row,
                            )
                        except Exception as _inv_close_exc:
                            logger.error(
                                f"[close_session] inventory deduct failed for order "
                                f"{ord_row['id']}: {_inv_close_exc}"
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
                        cash_required = float(session_total_for_check or 0)
                        if _minimum_close_state and _minimum_close_state["advance_total"] > 0:
                            cash_required = float(_minimum_close_state["overage_due"])
                        if cash_received < cash_required:
                            raise APIError(
                                f"Efectivo recibido ({cash_received}) debe ser mayor o igual al total a cobrar ({cash_required})",
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
                    if tip_amount > 0:
                        _mesa_tip_taxable = bool(tip_taxable)
                        tenant_tip_enabled = await conn.fetchval(
                            "SELECT tip_enabled FROM tenant_public_profiles WHERE tenant_id = $1",
                            tenant_id,
                        )
                        if not bool(tenant_tip_enabled):
                            raise APIError("Tipping is not enabled for this tenant", status_code=400)
                        tax_config = await _get_tenant_tax_config(conn, tenant_id)
                        _mesa_tip_tax_amount = compute_tip_tax_amount(
                            float(tip_amount), _mesa_tip_taxable, tax_config,
                        )
                        if not split_mode and payment_method == 'cash' and cash_received is not None:
                            session_total_with_tip = await conn.fetchval(
                                "SELECT COALESCE(SUM(total_amount), 0) FROM orders WHERE table_session_id = $1 AND status = 'completed'",
                                session_row["id"],
                            )
                            _required_cash = float(session_total_with_tip or 0) + tip_settlement_total(
                                float(tip_amount), _mesa_tip_tax_amount,
                            )
                            if _minimum_close_state and _minimum_close_state["advance_total"] > 0:
                                _required_cash = max(
                                    0.0,
                                    float(session_total_with_tip or 0)
                                    + tip_settlement_total(float(tip_amount), _mesa_tip_tax_amount)
                                    - float(_minimum_close_state["advance_total"]),
                                )
                            if cash_received < _required_cash:
                                raise APIError(
                                    f"Efectivo recibido ({cash_received}) debe cubrir total + propina"
                                    f" (+ IVA propina si aplica) ({_required_cash})",
                                    status_code=400,
                                )
                        await conn.execute(
                            """
                            UPDATE orders SET tip_amount = $1, tip_source = $2,
                                              tip_taxable = $4, tip_tax_amount = $5
                             WHERE id = (
                               SELECT id FROM orders
                                WHERE table_session_id = $3 AND status = 'completed'
                                ORDER BY created_at LIMIT 1
                             )
                            """,
                            float(tip_amount),
                            tip_source,
                            session_row["id"],
                            _mesa_tip_taxable,
                            float(_mesa_tip_tax_amount),
                        )

                    # Full-session wallet debit only for non-split closes.
                    # In split_mode each order_payments row applies its portion (#2020).
                    if payment_method == 'customer_wallet' and customer_id and not split_mode:
                        from app.services.customer_wallet_service import (
                            apply_wallet_for_session_orders,
                        )
                        from decimal import Decimal as _Dec

                        _mesa_orders = await conn.fetch(
                            """
                            SELECT id, total_amount
                            FROM orders
                            WHERE table_session_id = $1 AND status = 'completed'
                            ORDER BY created_at
                            """,
                            session_row["id"],
                        )
                        _tip_settlement = _Dec("0")
                        if float(tip_amount or 0) > 0:
                            _tip_settlement = _Dec(str(tip_settlement_total(
                                float(tip_amount), float(_mesa_tip_tax_amount),
                            )))
                        await apply_wallet_for_session_orders(
                            conn,
                            UUID(str(customer_id)),
                            UUID(str(tenant_id)),
                            _mesa_orders,
                            _tip_settlement,
                            UUID(str(session_context.user_id)) if session_context.user_id else None,
                        )

                    # GL journal entries — one per order, atomic with session close.
                    # Split closes wait until order_payments exist and the session
                    # is fully paid so debit lines can follow each tender account.
                    try:
                        completed_orders = await conn.fetch(
                            "SELECT id, order_number, total_amount, payment_method, payment_method_id, order_date "
                            "FROM orders WHERE table_session_id = $1 AND status = 'completed'",
                            session_row["id"],
                        )
                        if not split_mode and _advance_apply_amount > 0:
                            _advance_applied_total = await apply_session_advances_for_close(
                                conn,
                                UUID(str(tenant_id)),
                                session_row["id"],
                                _advance_apply_amount,
                                [row["id"] for row in completed_orders],
                            )
                        tax_config = await _get_tenant_tax_config(conn, tenant_id)
                        _tip_settlement_decimal = Decimal(str(
                            tip_settlement_total(float(tip_amount), _mesa_tip_tax_amount)
                        )).quantize(Decimal("0.01"))
                        _advance_remaining_after_products = max(
                            available_advance_total - _advance_applied_total,
                            Decimal("0"),
                        )
                        if not split_mode and _advance_remaining_after_products > 0 and _tip_settlement_decimal > 0:
                            _advance_tip_apply_amount = min(
                                _advance_remaining_after_products,
                                _tip_settlement_decimal,
                            )
                            _advance_tip_applied_total = await apply_session_advances_for_close(
                                conn,
                                UUID(str(tenant_id)),
                                session_row["id"],
                                _advance_tip_apply_amount,
                                [row["id"] for row in completed_orders],
                            )
                        _advance_remaining_after_settlement = max(
                            _advance_remaining_after_products - _advance_tip_applied_total,
                            Decimal("0"),
                        )
                        if not split_mode and _advance_remaining_after_settlement > 0:
                            _advance_cover_total = await recognize_unconsumed_advance_cover_for_close(
                                conn,
                                UUID(str(tenant_id)),
                                session_row["id"],
                                _advance_remaining_after_settlement,
                                [row["id"] for row in completed_orders],
                                tax_config,
                                date.today(),
                                UUID(str(user_id)) if user_id else None,
                            )
                        first_order_id = await conn.fetchval(
                            """
                            SELECT id FROM orders
                             WHERE table_session_id = $1 AND status = 'completed'
                             ORDER BY created_at LIMIT 1
                            """,
                            session_row["id"],
                        )
                        _advance_remaining_for_gl = _advance_applied_total + _advance_tip_applied_total
                        if not split_mode:
                            for ord_row in completed_orders:
                                ord_tip = Decimal("0")
                                ord_tip_tax = Decimal("0")
                                ord_advance = Decimal("0")
                                if first_order_id and ord_row["id"] == first_order_id:
                                    ord_tip = Decimal(str(tip_amount or 0))
                                    ord_tip_tax = Decimal(str(_mesa_tip_tax_amount))
                                if _advance_remaining_for_gl > 0:
                                    ord_settlement = (
                                        Decimal(str(ord_row["total_amount"] or 0))
                                        + ord_tip
                                        + ord_tip_tax
                                    )
                                    ord_advance = min(_advance_remaining_for_gl, ord_settlement)
                                    _advance_remaining_for_gl -= ord_advance
                                await _post_order_gl_entry(
                                    conn=conn,
                                    tenant_id=tenant_id,
                                    order_id=ord_row["id"],
                                    order_date=local_date_for_tenant(ord_row["order_date"], timezone_name),
                                    total_amount=Decimal(str(ord_row["total_amount"])),
                                    payment_method=ord_row["payment_method"] or payment_method or "digital",
                                    payment_method_id=ord_row["payment_method_id"],
                                    tax_config=tax_config,
                                    order_number=int(ord_row["order_number"]),
                                    tip_amount=ord_tip,
                                    tip_tax_amount=ord_tip_tax,
                                    advance_amount=ord_advance,
                                )
                                await _post_order_cogs_gl_entry(
                                    conn=conn,
                                    tenant_id=tenant_id,
                                    order_id=ord_row["id"],
                                    order_date=local_date_for_tenant(ord_row["order_date"], timezone_name),
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
                                if (
                                    payment_method == "customer_wallet"
                                    and customer_id
                                    and float(portion) > 0
                                ):
                                    from app.services.customer_wallet_service import (
                                        apply_wallet_for_order,
                                    )
                                    from decimal import Decimal as _Dec

                                    await apply_wallet_for_order(
                                        conn,
                                        UUID(str(customer_id)),
                                        UUID(str(tenant_id)),
                                        _Dec(str(portion)),
                                        ord_row["id"],
                                        UUID(str(user_id)) if user_id else None,
                                        inserted_row["id"],
                                    )

                        # Split GL when first payment completes the session.
                        first_tip_order = await conn.fetchrow(
                            """
                            SELECT id, order_number,
                                   COALESCE(tip_amount, 0) AS tip_amount,
                                   COALESCE(tip_tax_amount, 0) AS tip_tax_amount
                            FROM orders
                            WHERE table_session_id = $1 AND status = 'completed'
                            ORDER BY created_at LIMIT 1
                            """,
                            session_row["id"],
                        )
                        split_order_ids = [r["id"] for r in order_rows]
                        split_paid_row = await conn.fetchrow(
                            "SELECT COALESCE(SUM(amount), 0) AS paid FROM order_payments WHERE order_id = ANY($1) AND voided_at IS NULL",
                            split_order_ids,
                        )
                        split_paid_total = float(split_paid_row["paid"])
                        split_tip_amount = float(first_tip_order["tip_amount"]) if first_tip_order else 0.0
                        split_tip_tax_amount = float(first_tip_order["tip_tax_amount"]) if first_tip_order else 0.0
                        split_amount_due = split_settlement_amount_due(
                            session_total,
                            split_tip_amount,
                            split_tip_tax_amount,
                        )
                        from app.services.credit_service import sync_order_split_credit_status
                        _first_split_complete = (split_amount_due - split_paid_total) <= 0.01
                        for _sync_ord in order_rows:
                            await sync_order_split_credit_status(
                                conn,
                                _sync_ord["id"],
                                settlement_complete=_first_split_complete,
                            )
                        if split_amount_due - split_paid_total <= 0.01:
                            try:
                                split_tax_config = await _get_tenant_tax_config(conn, tenant_id)
                                split_completed_orders = await conn.fetch(
                                    """
                                    SELECT id, order_number, total_amount, payment_method,
                                           payment_method_id, order_date
                                    FROM orders
                                    WHERE table_session_id = $1 AND status = 'completed'
                                    ORDER BY created_at
                                    """,
                                    session_row["id"],
                                )
                                for split_ord in split_completed_orders:
                                    # Tip credits come from tender excess per order (exact splits).
                                    # Full session tip is not forced onto one order (avoids DR≠CR).
                                    await _post_order_gl_entry(
                                        conn=conn,
                                        tenant_id=tenant_id,
                                        order_id=split_ord["id"],
                                        order_date=local_date_for_tenant(split_ord["order_date"], timezone_name),
                                        total_amount=Decimal(str(split_ord["total_amount"])),
                                        payment_method=split_ord["payment_method"] or payment_method or "digital",
                                        payment_method_id=split_ord["payment_method_id"],
                                        tax_config=split_tax_config,
                                        order_number=int(split_ord["order_number"]),
                                        payment_splits=await _order_payment_splits_for_gl(conn, split_ord["id"]),
                                    )
                                    await _post_order_cogs_gl_entry(
                                        conn=conn,
                                        tenant_id=tenant_id,
                                        order_id=split_ord["id"],
                                        order_date=local_date_for_tenant(split_ord["order_date"], timezone_name),
                                        order_number=int(split_ord["order_number"]),
                                    )
                                if first_tip_order and split_tip_amount > 0:
                                    await _post_deferred_order_tip_gl(
                                        conn=conn,
                                        tenant_id=tenant_id,
                                        order_id=first_tip_order["id"],
                                        tip_amount=Decimal(str(first_tip_order["tip_amount"])),
                                        tip_tax_amount=Decimal(str(first_tip_order["tip_tax_amount"])),
                                        payment_method=payment_method or "digital",
                                        payment_method_id=payment_method_id,
                                        tax_config=split_tax_config,
                                        order_number=int(first_tip_order["order_number"]),
                                    )
                            except Exception as _split_gl_exc:
                                logger.error(
                                    f"Split GL failed for mesa session {session_row['id']}: {_split_gl_exc}"
                                )

                else:
                    completed_count = 0

                pending_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM orders WHERE table_session_id = $1 AND status = 'pending'",
                    session_row["id"],
                )

                if not split_mode and not payment_method:
                    pending_line_count = await conn.fetchval(
                        """
                        SELECT COUNT(*) FROM order_items oi
                        JOIN orders o ON o.id = oi.order_id
                        WHERE o.table_session_id = $1 AND o.status = 'pending'
                        """,
                        session_row["id"],
                    )
                    if pending_line_count and pending_line_count > 0:
                        if not _normalize_audit_reason(reason):
                            raise APIError(
                                "Motivo requerido para liberar la mesa con productos pendientes",
                                status_code=400,
                            )
                        await _record_tab_cleared_pending_lines(
                            conn,
                            tenant_id,
                            user_id=user_id,
                            table_id=table_id,
                            session_id=session_row["id"],
                            reason=reason,
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
                        minimum_snapshot = await _get_minimum_consumption_snapshot(conn, tenant_id)
                        await conn.execute(
                            """
                            INSERT INTO table_sessions (
                                table_id,
                                tenant_id,
                                opened_by_user_id,
                                minimum_consumption_enabled_snapshot,
                                minimum_consumption_amount_snapshot,
                                minimum_consumption_restrictive_snapshot
                            )
                            VALUES ($1, $2, NULL, $3, $4, $5)
                            """,
                            table_id,
                            tenant_id,
                            minimum_snapshot["enabled"],
                            minimum_snapshot["amount"],
                            minimum_snapshot["restrictive"],
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
                                conn=conn,
                                notify_print=False,
                            )

                        # Mesa: auto-deliver open comandas on payment. Barra: kitchen closes
                        # them manually (warocol.com#799).
                        if not is_bar_table:
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
                    """
                    SELECT id, order_number, total_amount
                    FROM orders
                    WHERE table_session_id = $1
                    ORDER BY created_at
                    """,
                    session_row["id"],
                )
                session_total = sum(float(r["total_amount"]) for r in order_rows)
                order_ids = [r["id"] for r in order_rows]
                paid_row = await conn2.fetchrow(
                    "SELECT COALESCE(SUM(amount), 0) AS paid FROM order_payments WHERE order_id = ANY($1) AND voided_at IS NULL",
                    order_ids,
                )
                paid_total = float(paid_row["paid"])
                
                session_tip_row = await conn2.fetchrow(
                    """
                    SELECT COALESCE(tip_amount, 0) AS tip_amount,
                           COALESCE(tip_tax_amount, 0) AS tip_tax_amount
                    FROM orders
                    WHERE table_session_id = $1 AND status = 'completed'
                    ORDER BY created_at LIMIT 1
                    """,
                    session_row["id"],
                )
                amount_due = split_settlement_amount_due(
                    session_total,
                    float(session_tip_row["tip_amount"] or 0) if session_tip_row else 0.0,
                    float(session_tip_row["tip_tax_amount"] or 0) if session_tip_row else 0.0,
                )
                remaining = max(0.0, amount_due - paid_total)
                is_complete = remaining <= 0.01
                completed_orders_payload = (
                    _completed_session_orders_payload(order_rows)
                    if is_complete else {}
                )
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
                    **completed_orders_payload,
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
            _liq_label = "IVA licores 5%"
            try:
                from app.services.hospitality_tax_engine import liquor_tax_label_for_config

                tax_config = await _get_tenant_tax_config(conn_ids, tenant_id)
                _liq_label = liquor_tax_label_for_config(tax_config)
                # Item-level rows (keep category_id / overrides). GROUP BY
                # tax_category alone collapses menu-mapped liquor into INC/IVA
                # when products still have legacy tax_category=standard
                # (warocol.com#2035).
                tax_rows = await conn_ids.fetch(
                    """
                    SELECT
                        COALESCE(p.tax_category, 'standard') AS tax_category,
                        COALESCE(p.tax_resolution, 'inherit') AS tax_resolution,
                        p.tax_line_key AS tax_line_key,
                        p.category_id::text AS category_id,
                        COALESCE(oi.net_total, oi.subtotal, 0) AS subtotal
                    FROM order_items oi
                    JOIN orders o ON o.id = oi.order_id
                    JOIN product p ON p.id = oi.product_id
                    WHERE o.id = ANY($1::uuid[])
                    """,
                    [r["id"] for r in order_rows],
                )
                _std_tax, _liq_tax, _tax_label = _compute_tax_breakdown(tax_rows, tax_config)
            except Exception as _e:
                logger.warning(f"Tax breakdown failed for mesa close (table {table_id}): {_e}")
        order_ids = [str(r["id"]) for r in order_rows]
        order_numbers = [int(r["order_number"]) for r in order_rows if r["order_number"] is not None]
        settlement_total = (
            float(sum(float(r.get("total_amount", 0)) for r in order_rows))
            + tip_settlement_total(float(tip_amount), _mesa_tip_tax_amount)
        )
        advance_settlement_applied = min(
            settlement_total,
            float(_advance_applied_total + _advance_tip_applied_total),
        )
        charged_amount = max(0.0, settlement_total - advance_settlement_applied)

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
                "liquor_tax_label": _liq_label,
                "promo_savings": float(_promo_savings),
                "promo_breakdown": _promo_breakdown,
                "payment_method": payment_method,
                "payment_method_id": str(payment_method_id) if payment_method_id else None,
                "advance_applied": advance_settlement_applied,
                "advance_cover_recognized": float(_advance_cover_total),
                "minimum_consumption": {
                    **(_minimum_close_state or {}),
                    "advance_applied": float(_advance_applied_total),
                    "advance_tip_applied": float(_advance_tip_applied_total),
                    "cover_recognized": float(_advance_cover_total),
                } if _minimum_close_state else None,
                "tip_amount": float(tip_amount) if tip_amount > 0 else 0,
                "tip_source": tip_source,
                "tip_taxable": _mesa_tip_taxable if tip_amount > 0 else False,
                "tip_tax_amount": float(_mesa_tip_tax_amount) if tip_amount > 0 else 0,
                "charged_amount": charged_amount if tip_amount > 0 or advance_settlement_applied > 0 else None,
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
    tip_amount: Optional[float] = None,
    tip_source: Optional[str] = None,
    tip_taxable: Optional[bool] = None,
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
            timezone_name = await resolve_tenant_timezone(conn, tenant_id)
            async with conn.transaction():
                # Get open session
                session_row = await conn.fetchrow(
                    "SELECT id FROM table_sessions WHERE table_id = $1 AND tenant_id = $2 AND closed_at IS NULL FOR UPDATE",
                    table_id, tenant_id,
                )
                if not session_row:
                    raise NotFoundError("No open session found for this table")

                await _consolidate_session_orders_for_checkout(conn, session_row["id"])

                # Get all completed (partial) orders for this session
                order_rows = await conn.fetch(
                    """
                    SELECT id, order_number, total_amount, payment_method,
                           payment_method_id, order_date, customer_id
                    FROM orders
                    WHERE table_session_id = $1 AND status = 'completed'
                    ORDER BY created_at
                    """,
                    session_row["id"],
                )
                if not order_rows:
                    raise APIError("No split payment orders found for this session — call close with split_mode=True first", status_code=400)
                session_customer_id = next(
                    (r["customer_id"] for r in order_rows if r["customer_id"]),
                    None,
                )
                if payment_method == "customer_wallet" and not session_customer_id:
                    raise APIError(
                        "La billetera requiere un cliente en la mesa",
                        status_code=400,
                    )

                session_total = sum(float(r["total_amount"]) for r in order_rows)
                order_ids = [r["id"] for r in order_rows]
                session_tip_row = await conn.fetchrow(
                    """
                    SELECT id,
                           COALESCE(tip_amount, 0) AS tip_amount,
                           COALESCE(tip_source, 'none') AS tip_source,
                           COALESCE(tip_taxable, false) AS tip_taxable,
                           COALESCE(tip_tax_amount, 0) AS tip_tax_amount
                    FROM orders
                    WHERE table_session_id = $1 AND status = 'completed'
                    ORDER BY created_at
                    LIMIT 1
                    FOR UPDATE
                    """,
                    session_row["id"],
                )
                resolved_tip_amount = float(session_tip_row["tip_amount"] or 0) if session_tip_row else 0.0
                resolved_tip_source = session_tip_row["tip_source"] if session_tip_row else "none"
                resolved_tip_taxable = bool(session_tip_row["tip_taxable"]) if session_tip_row else False
                resolved_tip_tax_amount = float(session_tip_row["tip_tax_amount"] or 0) if session_tip_row else 0.0

                if any(value is not None for value in (tip_amount, tip_source, tip_taxable)):
                    try:
                        resolved_tip_amount, resolved_tip_source, resolved_tip_taxable = normalize_tip_payload(
                            resolved_tip_amount if tip_amount is None else tip_amount,
                            resolved_tip_source if tip_source is None else tip_source,
                            resolved_tip_taxable if tip_taxable is None else tip_taxable,
                        )
                    except ValueError as exc:
                        raise APIError(str(exc), status_code=400)

                    if resolved_tip_amount > 0:
                        tip_enabled = await conn.fetchval(
                            "SELECT tip_enabled FROM tenant_public_profiles WHERE tenant_id = $1",
                            tenant_id,
                        )
                        if not bool(tip_enabled):
                            raise APIError("Tipping is not enabled for this tenant", status_code=400)
                        tax_config = await _get_tenant_tax_config(conn, tenant_id)
                        resolved_tip_tax_amount = compute_tip_tax_amount(
                            resolved_tip_amount, resolved_tip_taxable, tax_config,
                        )
                    else:
                        resolved_tip_tax_amount = 0.0

                    await conn.execute(
                        """
                        UPDATE orders
                        SET tip_amount = $2,
                            tip_source = $3,
                            tip_taxable = $4,
                            tip_tax_amount = $5
                        WHERE id = $1
                        """,
                        session_tip_row["id"],
                        resolved_tip_amount,
                        resolved_tip_source,
                        resolved_tip_taxable,
                        resolved_tip_tax_amount,
                    )

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
                    if (
                        payment_method == "customer_wallet"
                        and session_customer_id
                        and float(portion) > 0
                    ):
                        from app.services.customer_wallet_service import apply_wallet_for_order
                        from decimal import Decimal as _Dec

                        await apply_wallet_for_order(
                            conn,
                            UUID(str(session_customer_id)),
                            UUID(str(tenant_id)),
                            _Dec(str(portion)),
                            ord_row["id"],
                            UUID(str(user_id)) if user_id else None,
                            inserted_row["id"],
                        )

                # Recompute paid total
                paid_row = await conn.fetchrow(
                    "SELECT COALESCE(SUM(amount), 0) AS paid FROM order_payments WHERE order_id = ANY($1) AND voided_at IS NULL",
                    order_ids,
                )
                paid_total = float(paid_row["paid"])
                
                amount_due = split_settlement_amount_due(
                    session_total,
                    resolved_tip_amount,
                    resolved_tip_tax_amount,
                )
                remaining = max(0.0, amount_due - paid_total)
                is_complete = remaining <= 0.01

                from app.services.credit_service import sync_order_split_credit_status
                for _sync_ord in order_rows:
                    await sync_order_split_credit_status(
                        conn,
                        _sync_ord["id"],
                        settlement_complete=is_complete,
                    )

                if is_complete:
                    # Session settlement complete — credit tenders stay partial/credit via sync above (#2020).
                    await conn.execute(
                        """
                        UPDATE orders
                        SET payment_method = $2,
                            payment_method_id = $3::uuid
                        WHERE table_session_id = $1 AND status = 'completed'
                        """,
                        session_row["id"],
                        payment_method,
                        payment_method_id,
                    )
                    try:
                        split_tax_config = await _get_tenant_tax_config(conn, tenant_id)
                        for ord_row in order_rows:
                            # Tip from tender excess per order; deferred tip below is idempotent.
                            await _post_order_gl_entry(
                                conn=conn,
                                tenant_id=tenant_id,
                                order_id=ord_row["id"],
                                order_date=local_date_for_tenant(ord_row["order_date"], timezone_name),
                                total_amount=Decimal(str(ord_row["total_amount"])),
                                payment_method=ord_row["payment_method"] or payment_method or "digital",
                                payment_method_id=ord_row["payment_method_id"],
                                tax_config=split_tax_config,
                                order_number=int(ord_row["order_number"]),
                                payment_splits=await _order_payment_splits_for_gl(conn, ord_row["id"]),
                            )
                            await _post_order_cogs_gl_entry(
                                conn=conn,
                                tenant_id=tenant_id,
                                order_id=ord_row["id"],
                                order_date=local_date_for_tenant(ord_row["order_date"], timezone_name),
                                order_number=int(ord_row["order_number"]),
                            )
                    except Exception as _split_gl_exc:
                        logger.error(
                            f"Split GL failed for mesa table {table_id}: {_split_gl_exc}"
                        )
                    if resolved_tip_amount > 0 and session_tip_row:
                        try:
                            tip_order_meta = await conn.fetchrow(
                                "SELECT order_number FROM orders WHERE id = $1",
                                session_tip_row["id"],
                            )
                            tip_tax_config = await _get_tenant_tax_config(conn, tenant_id)
                            await _post_deferred_order_tip_gl(
                                conn=conn,
                                tenant_id=tenant_id,
                                order_id=session_tip_row["id"],
                                tip_amount=Decimal(str(resolved_tip_amount)),
                                tip_tax_amount=Decimal(str(resolved_tip_tax_amount)),
                                payment_method=payment_method,
                                payment_method_id=payment_method_id,
                                tax_config=tip_tax_config,
                                order_number=int(tip_order_meta["order_number"]) if tip_order_meta else None,
                            )
                        except Exception as _defer_tip_exc:
                            logger.error(
                                f"Deferred tip GL failed for mesa table {table_id}: {_defer_tip_exc}"
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
                completed_orders_payload = (
                    _completed_session_orders_payload(order_rows)
                    if is_complete else {}
                )

        return {
            "success": True,
            "data": {
                "session_id": str(session_row["id"]),
                "paid_total": paid_total,
                "remaining": remaining,
                "is_complete": is_complete,
                "tip_amount": resolved_tip_amount,
                "tip_source": resolved_tip_source,
                "tip_taxable": resolved_tip_taxable,
                "tip_tax_amount": resolved_tip_tax_amount,
                # Issue warocol.com#649 — real UUID; siblings void via heuristic.
                "payment_id": first_payment_id,
                **completed_orders_payload,
            },
        }

    except (AuthenticationError, NotFoundError, APIError):
        raise
    except Exception as e:
        logger.error(f"Error adding session payment for table {table_id}: {e}")
        raise APIError(f"Error adding session payment: {e}", status_code=500)


async def defer_tab_delivery_payment(
    request: Request,
    table_id: UUID,
    customer_id: UUID,
    delivery_address_id: UUID,
    delivery_instructions: Optional[str] = None,
) -> dict:
    """
    Assign customer and delivery metadata to the open bar tab without posting
    payment/accounting yet. The order stays pending and is finalized later from
    /ventas when the courier knows the tender.
    """
    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    instructions = (delivery_instructions or "").strip() or None

    async with get_db_connection() as conn:
        async with conn.transaction():
            session_row = await conn.fetchrow(
                """
                SELECT ts.id AS session_id, t.id AS table_id, t.is_bar, t.name AS table_name
                  FROM table_sessions ts
                  JOIN tables t ON t.id = ts.table_id
                 WHERE ts.table_id = $1
                   AND ts.tenant_id = $2
                   AND ts.closed_at IS NULL
                """,
                table_id,
                tenant_id,
            )
            if not session_row:
                raise NotFoundError("No hay una cuenta abierta para esta barra")
            if not session_row["is_bar"]:
                raise APIError("Esta acción solo aplica para barra", status_code=400)

            customer_phone = await conn.fetchval(
                "SELECT phone_number FROM profile WHERE id = $1",
                customer_id,
            )
            if customer_phone is None:
                raise NotFoundError("Cliente no encontrado")
            if customer_phone == "0000000000":
                raise APIError(
                    "El domicilio requiere un cliente identificado (no anónimo)",
                    status_code=400,
                )

            address_ok = await conn.fetchval(
                """
                SELECT 1
                  FROM addresses_profile
                 WHERE id = $1
                   AND user_id = $2
                   AND deleted_at IS NULL
                """,
                delivery_address_id,
                customer_id,
            )
            if not address_ok:
                raise APIError(
                    "Dirección de entrega no válida o no pertenece al cliente",
                    status_code=400,
                )

            order_row = await conn.fetchrow(
                """
                UPDATE orders
                   SET customer_id = $1,
                       delivery_address_id = $2,
                       delivery_instructions = $3,
                       payment_method = NULL,
                       payment_method_id = NULL,
                       payment_status = NULL
                 WHERE id = (
                       SELECT id
                         FROM orders
                        WHERE table_session_id = $4
                          AND tenant_id = $5
                          AND status = 'pending'
                        ORDER BY order_date ASC
                        LIMIT 1
                 )
                RETURNING id, order_number, total_amount, status, payment_status
                """,
                customer_id,
                delivery_address_id,
                instructions,
                session_row["session_id"],
                tenant_id,
            )
            if not order_row:
                raise APIError("No hay productos pendientes en esta cuenta", status_code=400)

            await conn.execute(
                "UPDATE table_sessions SET closed_at = now() WHERE id = $1",
                session_row["session_id"],
            )
            minimum_snapshot = await _get_minimum_consumption_snapshot(conn, tenant_id)
            new_session_row = await conn.fetchrow(
                """
                INSERT INTO table_sessions (
                    table_id,
                    tenant_id,
                    opened_by_user_id,
                    minimum_consumption_enabled_snapshot,
                    minimum_consumption_amount_snapshot,
                    minimum_consumption_restrictive_snapshot
                )
                VALUES ($1, $2, NULL, $3, $4, $5)
                RETURNING id
                """,
                table_id,
                tenant_id,
                minimum_snapshot["enabled"],
                minimum_snapshot["amount"],
                minimum_snapshot["restrictive"],
            )

    return {
        "success": True,
        "message": "Venta guardada como pendiente",
        "data": {
            "order_id": str(order_row["id"]),
            "order_number": order_row["order_number"],
            "total_amount": float(order_row["total_amount"]),
            "status": order_row["status"],
            "payment_status": order_row["payment_status"],
            "payment_method": None,
            "delivery_address_id": str(delivery_address_id),
            "next_table_session_id": str(new_session_row["id"]),
        },
    }


_PENDING_DELIVERY_PAYMENT_STATUSES = {None, "unpaid", "pending"}

# completed+paid in DB but zero order_payments — inconsistent legacy rows, not POS-collectible
_PENDING_DELIVERY_ZOMBIE_SQL = """
              AND NOT (
                  o.status = 'completed'
                  AND o.payment_status = 'paid'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM order_payments op
                      WHERE op.order_id = o.id AND op.voided_at IS NULL
                  )
              )
"""


def _is_pending_delivery_zombie(order: dict, *, payment_count: int) -> bool:
    return (
        order.get("status") == "completed"
        and order.get("payment_status") == "paid"
        and payment_count <= 0
    )


def _pending_delivery_amount_due(order: dict) -> float:
    return round(
        float(order.get("total_amount") or 0)
        + float(order.get("tip_amount") or 0)
        + float(order.get("tip_tax_amount") or 0),
        2,
    )


def _is_pending_delivery_candidate(order: dict) -> bool:
    """Bar delivery order that may appear in the POS domicilios queue."""
    if order.get("source") != "barra":
        return False
    if not (order.get("is_delivery") or order.get("delivery_address_id")):
        return False
    return order.get("status") not in ("cancelled", "refunded")


def _is_collectible_pending_delivery(order: dict, *, outstanding: float | None = None) -> bool:
    """Pending bar delivery still owed at POS (split in progress or not yet started).

    Credit-mixed splits keep ``payment_status='partial'`` for Cartera even when
    the cashier has collected every tender; use outstanding balance, not status.
    """
    if not _is_pending_delivery_candidate(order):
        return False
    if outstanding is not None:
        return outstanding > 0.01
    if order.get("status") == "pending":
        return order.get("payment_status") in _PENDING_DELIVERY_PAYMENT_STATUSES
    if order.get("status") == "completed":
        return order.get("payment_status") == "partial"
    return False


async def _pending_delivery_outstanding(conn, order_id: UUID, order: dict) -> float:
    paid_row = await conn.fetchrow(
        """
        SELECT COALESCE(SUM(amount), 0) AS paid_total
        FROM order_payments
        WHERE order_id = $1 AND voided_at IS NULL
        """,
        order_id,
    )
    paid_total = round(float(paid_row["paid_total"] or 0), 2)
    amount_due = _pending_delivery_amount_due(order)
    return max(0.0, round(amount_due - paid_total, 2))


def _is_unpaid_pending_delivery(order: dict) -> bool:
    return _is_collectible_pending_delivery(order)


def _serialize_pending_delivery_row(row) -> dict:
    address_parts = [row["address_line1"], row["address_line2"], row["city"]]
    address_label = ", ".join(part for part in address_parts if part)
    return {
        "id": str(row["id"]),
        "order_number": int(row["order_number"]),
        "order_date": row["order_date"].isoformat() if row["order_date"] else None,
        "total_amount": float(row["total_amount"] or 0),
        "status": row["status"],
        "payment_status": row["payment_status"],
        "delivery_instructions": row["delivery_instructions"],
        "customer": {
            "id": str(row["customer_id"]) if row["customer_id"] else None,
            "name": row["customer_name"],
            "phone_number": row["customer_phone"],
        },
        "address_label": address_label or None,
        "delivery_address_id": str(row["delivery_address_id"]) if row["delivery_address_id"] else None,
    }


async def list_pending_deliveries(request: Request) -> dict:
    """POS queue of unpaid pending delivery orders deferred from barra."""
    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    zombie_sql = _PENDING_DELIVERY_ZOMBIE_SQL.strip()
    async with get_db_connection() as conn:
        rows = await conn.fetch(
            f"""
            SELECT
                o.id,
                o.order_number,
                o.order_date,
                o.total_amount,
                o.status,
                o.payment_status,
                o.delivery_instructions,
                o.delivery_address_id,
                p.id AS customer_id,
                p.name AS customer_name,
                p.phone_number AS customer_phone,
                ap.address_line1,
                ap.address_line2,
                ap.city
            FROM orders o
            INNER JOIN table_sessions ts ON ts.id = o.table_session_id
            INNER JOIN tables t ON t.id = ts.table_id AND t.is_bar = TRUE
            LEFT JOIN profile p ON p.id = o.customer_id
            LEFT JOIN addresses_profile ap
              ON ap.id = o.delivery_address_id AND ap.deleted_at IS NULL
            WHERE o.tenant_id = $1
              AND o.delivery_address_id IS NOT NULL
              AND o.status NOT IN ('cancelled', 'refunded')
              AND (
                  SELECT COALESCE(SUM(op.amount), 0)
                  FROM order_payments op
                  WHERE op.order_id = o.id AND op.voided_at IS NULL
              ) < (
                  o.total_amount
                  + COALESCE(o.tip_amount, 0)
                  + COALESCE(o.tip_tax_amount, 0)
                  - 0.01
              )
            {zombie_sql}
            ORDER BY o.order_date DESC
            """,
            tenant_id,
        )

    return {
        "success": True,
        "data": [_serialize_pending_delivery_row(row) for row in rows],
    }


async def get_pending_delivery(request: Request, order_id: UUID) -> dict:
    """Load a pending unpaid delivery for POS checkout."""
    order_payload = await get_order_by_id(request, order_id)
    order = order_payload.get("data") or {}
    if not _is_pending_delivery_candidate(order):
        raise APIError("Este domicilio ya no está pendiente de cobro", status_code=409)

    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id
    async with get_db_connection(use_transaction=False) as conn:
        payment_facts = await conn.fetchrow(
            """
            SELECT COUNT(*)::int AS payment_count
            FROM order_payments
            WHERE order_id = $1 AND voided_at IS NULL
            """,
            order_id,
        )
        payment_count = int(payment_facts["payment_count"] or 0)
        if _is_pending_delivery_zombie(order, payment_count=payment_count):
            raise APIError(
                "Esta venta figura como cobrada pero no tiene pagos registrados. Revísala en Ventas.",
                status_code=409,
                details={"code": "pending_delivery_zombie"},
            )
        outstanding = await _pending_delivery_outstanding(conn, order_id, order)
    if outstanding <= 0.01:
        raise APIError("Este domicilio ya no está pendiente de cobro", status_code=409)

    items_payload = await get_order_items(request, order_id)
    partial_payments: list[dict] = []
    if tenant_id:
        async with get_db_connection(use_transaction=False) as conn:
            partial_rows = await conn.fetch(
                """
                SELECT op.id, op.amount, op.payment_method, op.payment_method_id,
                       pm.name AS payment_method_name
                FROM order_payments op
                LEFT JOIN payment_methods pm ON pm.id = op.payment_method_id
                WHERE op.order_id = $1
                  AND op.tenant_id = $2
                  AND op.voided_at IS NULL
                ORDER BY op.paid_at, op.id
                """,
                order_id,
                tenant_id,
            )
            partial_payments = [
                {
                    "id": str(row["id"]),
                    "amount": float(row["amount"]),
                    "payment_method": row["payment_method"],
                    "payment_method_id": str(row["payment_method_id"]) if row["payment_method_id"] else None,
                    "payment_method_name": row["payment_method_name"],
                }
                for row in partial_rows
            ]
    return {
        "success": True,
        "data": {
            **order,
            "items": items_payload.get("data") or [],
            "partial_payments": partial_payments,
        },
    }


async def complete_pending_delivery(
    request: Request,
    order_id: UUID,
    *,
    payment_method: Optional[str] = None,
    payment_method_id: Optional[str] = None,
    customer_id: Optional[str] = None,
    cash_received: Optional[float] = None,
    credit_due_date: Optional[date] = None,
    served_by_member_id: Optional[UUID] = None,
    discount_type: Optional[str] = None,
    discount_value: Optional[float] = None,
    tip_amount: Optional[float] = None,
    tip_source: Optional[str] = None,
    tip_taxable: Optional[bool] = None,
    waros_to_redeem: Optional[int] = None,
    waro_reward_id: Optional[UUID] = None,
    wompi_collection: bool = False,
    split_mode: bool = False,
    split_first_amount: float = 0.0,
    split_first_cash_received: Optional[float] = None,
) -> dict:
    """Collect payment on a pending delivery from POS checkout."""
    detail = await get_pending_delivery(request, order_id)
    order = detail["data"]
    if split_mode and wompi_collection:
        raise APIError("Wompi no admite cobro dividido", status_code=400)
    await update_order_status(
        request,
        order_id,
        "completed",
        payment_method,
        payment_method_id,
        customer_id or (order.get("customer") or {}).get("id"),
        None,
        cash_received=cash_received,
        credit_due_date=credit_due_date,
        served_by_member_id=served_by_member_id,
        discount_type=discount_type,
        discount_value=discount_value,
        tip_amount=tip_amount,
        tip_source=tip_source,
        tip_taxable=tip_taxable,
        waros_to_redeem=waros_to_redeem,
        waro_reward_id=waro_reward_id,
        wompi_collection=wompi_collection,
        split_mode=split_mode,
        split_first_amount=split_first_amount,
        split_first_cash_received=split_first_cash_received,
    )
    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id
    paid_total = 0.0
    remaining = 0.0
    is_complete = not split_mode
    payment_id: Optional[str] = None
    if tenant_id:
        async with get_db_connection(use_transaction=False) as conn:
            order_row = await conn.fetchrow(
                """
                SELECT total_amount, tip_amount, tip_tax_amount, status, payment_status
                FROM orders
                WHERE id = $1 AND tenant_id = $2
                """,
                order_id,
                tenant_id,
            )
            paid_row = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(amount), 0) AS paid
                FROM order_payments
                WHERE order_id = $1 AND voided_at IS NULL
                """,
                order_id,
            )
            last_payment = await conn.fetchrow(
                """
                SELECT id
                FROM order_payments
                WHERE order_id = $1 AND voided_at IS NULL
                ORDER BY paid_at DESC, id DESC
                LIMIT 1
                """,
                order_id,
            )
            if order_row and paid_row:
                amount_due = split_settlement_amount_due(
                    float(order_row["total_amount"] or 0),
                    float(order_row["tip_amount"] or 0),
                    float(order_row["tip_tax_amount"] or 0),
                )
                paid_total = float(paid_row["paid"])
                remaining = max(0.0, amount_due - paid_total)
                is_complete = remaining <= 0.01 or order_row["payment_status"] == "paid"
            if last_payment:
                payment_id = str(last_payment["id"])
            if is_complete and not wompi_collection:
                await conn.execute(
                    """
                    UPDATE tables t
                       SET status = 'open'
                     WHERE t.tenant_id = $1
                       AND t.is_bar = TRUE
                       AND EXISTS (
                           SELECT 1
                             FROM table_sessions ts
                            WHERE ts.table_id = t.id
                              AND ts.closed_at IS NULL
                       )
                    """,
                    tenant_id,
                )
    return {
        "success": True,
        "message": "Domicilio cobrado",
        "data": {
            "order_id": order["id"],
            "order_number": order.get("order_number"),
            "total_amount": order.get("total_amount"),
            "status": "completed" if not wompi_collection else order.get("status"),
            "payment_status": None if wompi_collection else ("paid" if is_complete else "partial"),
            "payment_method": payment_method,
            "customer_id": (order.get("customer") or {}).get("id"),
            "standard_tax": order.get("standard_tax"),
            "liquor_tax": order.get("liquor_tax"),
            "standard_tax_label": order.get("standard_tax_label"),
            "liquor_tax_label": order.get("liquor_tax_label"),
            **(
                {
                    "paid_total": paid_total,
                    "remaining": remaining,
                    "is_complete": is_complete,
                    "payment_id": payment_id,
                }
                if split_mode
                else {}
            ),
        },
    }


async def add_pending_delivery_payment(
    request: Request,
    order_id: UUID,
    *,
    amount: float,
    payment_method: str,
    payment_method_id: Optional[str] = None,
    cash_received: Optional[float] = None,
) -> dict:
    """Add a follow-up tender while collecting a deferred bar delivery."""
    detail = await get_pending_delivery(request, order_id)
    order = detail["data"]
    if order.get("status") == "pending" and not detail["data"].get("partial_payments"):
        raise APIError(
            "Registra el primer pago con cobro parcial activo",
            status_code=400,
            details={"code": "pending_delivery_split_first_required"},
        )
    result = await add_order_payment(
        request=request,
        order_id=str(order_id),
        amount=amount,
        payment_method=payment_method,
        payment_method_id=payment_method_id,
        cash_received=cash_received,
    )
    if result.get("data", {}).get("is_complete"):
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if tenant_id:
            async with get_db_connection() as conn:
                await conn.execute(
                    """
                    UPDATE tables t
                       SET status = 'open'
                     WHERE t.tenant_id = $1
                       AND t.is_bar = TRUE
                       AND EXISTS (
                           SELECT 1
                             FROM table_sessions ts
                            WHERE ts.table_id = t.id
                              AND ts.closed_at IS NULL
                       )
                    """,
                    tenant_id,
                )
    return result


async def void_pending_delivery_payment(
    request: Request,
    order_id: UUID,
    payment_id: UUID,
    *,
    reason: Optional[str] = None,
) -> dict:
    """Void a partial tender on a deferred bar delivery checkout."""
    await get_pending_delivery(request, order_id)
    return await void_order_payment(
        request=request,
        order_id=str(order_id),
        payment_id=str(payment_id),
        reason=reason,
    )


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
                    ts.covers,
                    ts.capacity_snapshot,
                    ts.custom_label,
                    ts.minimum_consumption_enabled_snapshot,
                    ts.minimum_consumption_amount_snapshot,
                    ts.minimum_consumption_restrictive_snapshot,
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
            _liq_label = "IVA licores 5%"
            _promo_savings = 0.0
            _subtotal_after_promos = float(session_row["running_total"])
            _promo_breakdown: List[dict] = []
            _promo_lines_by_id: Dict[str, dict] = {}
            try:
                from app.services.hospitality_tax_engine import (
                    annotate_line_tax_amounts,
                    liquor_tax_label_for_config,
                )
                from app.services.orders_service import _compute_tax_breakdown
                from app.services.promotions_service import (
                    enrich_order_item_rows_with_promo_basis,
                    evaluate_checkout_promotions,
                    item_rows_to_promo_lines,
                )

                tax_config = await _get_tenant_tax_config(conn, tenant_id)
                _liq_label = liquor_tax_label_for_config(tax_config)
                order_ids = [o["id"] for o in orders]
                if order_ids:
                    eval_rows = await conn.fetch(
                        """
                        SELECT
                            oi.id,
                            oi.quantity,
                            oi.subtotal,
                            oi.price_at_purchase,
                            oi.product_id,
                            oi.promo_opt_out,
                            oi.applied_promotion_id AS locked_promotion_id,
                            tp.name AS locked_promotion_name,
                            tp.promo_type AS locked_promo_type,
                            oi.promo_savings_allocated AS locked_promo_savings,
                            p.category_id,
                            COALESCE(p.tax_category, 'standard') AS tax_category,
                            COALESCE(p.tax_resolution, 'inherit') AS tax_resolution,
                            p.tax_line_key
                        FROM order_items oi
                        JOIN orders o ON o.id = oi.order_id
                        JOIN product p ON p.id = oi.product_id
                        LEFT JOIN tenant_promotions tp ON tp.id = oi.applied_promotion_id
                        WHERE o.id = ANY($1::uuid[])
                        """,
                        order_ids,
                    )
                    eval_rows = await enrich_order_item_rows_with_promo_basis(conn, eval_rows)
                    promo_lines = item_rows_to_promo_lines(eval_rows)
                    checkout_eval = await evaluate_checkout_promotions(
                        conn,
                        UUID(str(tenant_id)),
                        promo_lines,
                        preserve_persisted_promos=True,
                    )
                    _promo_savings = float(checkout_eval.get("promo_savings") or 0)
                    _subtotal_after_promos = float(checkout_eval.get("subtotal_after_promos") or 0)
                    _promo_breakdown = checkout_eval.get("promo_breakdown") or []
                    tax_fields_by_id = {
                        str(row["id"]): {
                            "tax_category": row.get("tax_category") or "standard",
                            "category_id": (
                                str(row["category_id"]) if row.get("category_id") else None
                            ),
                            "tax_resolution": row.get("tax_resolution") or "inherit",
                            "tax_line_key": row.get("tax_line_key"),
                        }
                        for row in eval_rows
                    }
                    for line in checkout_eval["lines"]:
                        fields = tax_fields_by_id.get(str(line["id"]), {})
                        line["tax_category"] = fields.get("tax_category", "standard")
                        line["category_id"] = fields.get("category_id")
                        line["tax_resolution"] = fields.get("tax_resolution", "inherit")
                        line["tax_line_key"] = fields.get("tax_line_key")
                    tax_rows = _tax_rows_from_evaluated_lines(checkout_eval["lines"])
                    _std_tax, _liq_tax, _tax_label = _compute_tax_breakdown(tax_rows, tax_config)
                    annotate_line_tax_amounts(
                        checkout_eval["lines"],
                        tax_config,
                        reconcile_to=(float(_std_tax), float(_liq_tax)),
                    )
                    for line in checkout_eval["lines"]:
                        _promo_lines_by_id[str(line["id"])] = line
            except Exception as _e:
                logger.warning(f"Tax breakdown failed for mesa current session (table {table_id}): {_e}")

            # Fetch individual order items for this session (with IDs for edit/delete)
            tab_items = await conn.fetch(
                """
                SELECT
                    oi.id AS order_item_id,
                    oi.product_id,
                    oi.quantity,
                    oi.price_at_purchase,
                    oi.subtotal,
                    oi.notes,
                    oi.promo_opt_out,
                    oi.applied_promotion_id,
                    oi.promo_savings_allocated,
                    oi.fulfillment_status,
                    oi.sent_at,
                    p.name AS product_name,
                    p.category_id,
                    tp.name AS promotion_name,
                    tp.promo_type,
                    COALESCE(
                        json_agg(
                            json_build_object(
                                'id', oim.modifier_id::text,
                                'name', oim.modifier_name,
                                'price', oim.price_at_purchase,
                                'quantity', oim.quantity
                            ) ORDER BY oim.created_at
                        ) FILTER (WHERE oim.order_item_id IS NOT NULL),
                        '[]'::json
                    ) AS modifiers
                FROM order_items oi
                JOIN orders o ON o.id = oi.order_id
                JOIN product p ON p.id = oi.product_id
                LEFT JOIN tenant_promotions tp ON tp.id = oi.applied_promotion_id
                LEFT JOIN order_item_modifiers oim ON oim.order_item_id = oi.id
                WHERE o.table_session_id = $1
                GROUP BY
                    oi.id, oi.product_id, oi.quantity, oi.price_at_purchase, oi.subtotal,
                    oi.notes, oi.promo_opt_out, oi.applied_promotion_id, oi.promo_savings_allocated,
                    oi.fulfillment_status, oi.sent_at, p.name, p.category_id, tp.name, tp.promo_type
                ORDER BY oi.created_at ASC, oi.id ASC
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
            partial_paid_total = sum(float(r["group_amount"] or 0) for r in partial_payments_rows)
            from app.services.table_session_advances_service import get_session_advances_payload

            advances_payload = await get_session_advances_payload(
                conn,
                UUID(str(tenant_id)),
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
                    "subtotal_after_promos": _subtotal_after_promos,
                    "promo_savings": _promo_savings,
                    "promo_breakdown": _promo_breakdown,
                    "order_count": int(session_row["order_count"]),
                    "standard_tax": float(_std_tax),
                    "liquor_tax": float(_liq_tax),
                    "standard_tax_label": _tax_label,
                    "liquor_tax_label": _liq_label,
                    # Per-line tax for POS Orden / prefactura cues (#2007 mesa gap)
                    "lines": list(_promo_lines_by_id.values()),
                    "minimum_consumption": _minimum_consumption_state(
                        session_row,
                        partial_paid_total,
                        advances_payload["advance_totals"]["active_total_cop"],
                    ),
                    "session_advances": advances_payload["advances"],
                    "session_advance_totals": advances_payload["advance_totals"],
                    # Waiter attribution (warocol.com#574)
                    "attended_by_member_id": str(session_row["attended_by_member_id"]) if session_row.get("attended_by_member_id") else None,
                    "attended_by_member_name": session_row.get("attended_by_member_name"),
                    "attended_by_member_role": session_row.get("attended_by_member_role"),
                    "effective_waiter_member_id": str(session_row["effective_waiter_member_id"]) if session_row.get("effective_waiter_member_id") else None,
                    "effective_waiter_member_name": session_row.get("effective_waiter_member_name"),
                    "effective_waiter_member_role": session_row.get("effective_waiter_member_role"),
                    "covers": int(session_row["covers"]) if session_row.get("covers") is not None else None,
                    "capacity_snapshot": int(session_row["capacity_snapshot"]) if session_row.get("capacity_snapshot") is not None else None,
                    "custom_label": session_row.get("custom_label"),
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
                        "productId": str(r["product_id"]),
                        "categoryId": str(r["category_id"]) if r["category_id"] else None,
                        "productName": r["product_name"],
                        "quantity": r["quantity"],
                        "unitPrice": float(r["price_at_purchase"]),
                        "subtotal": float(r["subtotal"]),
                        "promoSavings": int(
                            _promo_lines_by_id.get(str(r["order_item_id"]), {}).get("promo_savings")
                            or r["promo_savings_allocated"]
                            or 0
                        ),
                        "promotionId": (
                            _promo_lines_by_id.get(str(r["order_item_id"]), {}).get("promotion_id")
                            or (str(r["applied_promotion_id"]) if r["applied_promotion_id"] else None)
                        ),
                        "promotionName": (
                            _promo_lines_by_id.get(str(r["order_item_id"]), {}).get("promotion_name")
                            or r["promotion_name"]
                        ),
                        "promoType": (
                            _promo_lines_by_id.get(str(r["order_item_id"]), {}).get("promo_type")
                            or r["promo_type"]
                        ),
                        "promoOptOut": bool(r.get("promo_opt_out")),
                        "taxCategory": (
                            _promo_lines_by_id.get(str(r["order_item_id"]), {}).get("tax_category")
                        ),
                        "taxLabel": (
                            _promo_lines_by_id.get(str(r["order_item_id"]), {}).get("tax_label")
                        ),
                        "taxAmount": (
                            _promo_lines_by_id.get(str(r["order_item_id"]), {}).get("tax_amount")
                        ),
                        "includedInPrice": (
                            _promo_lines_by_id.get(str(r["order_item_id"]), {}).get("included_in_price")
                        ),
                        "modifiers": [
                            {
                                "id": mod["id"],
                                "name": mod["name"],
                                "price": float(mod["price"]),
                                "quantity": int(mod.get("quantity") or 1),
                            }
                            for mod in (
                                json.loads(r["modifiers"])
                                if isinstance(r["modifiers"], str)
                                else (r["modifiers"] or [])
                            )
                        ],
                        "notes": r["notes"],
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


def _serialize_comanda_item(row: Any) -> Dict[str, Any]:
    item = _parse_item_row(row)
    return {
        "id": str(item["id"]),
        "order_item_id": str(item["order_item_id"]) if item.get("order_item_id") else None,
        "kitchen_name": item["kitchen_name"],
        "quantity": float(item["quantity"]),
        "notes": item.get("notes"),
        "modifiers_snapshot": item.get("modifiers_snapshot"),
        "is_promo_free": bool(item.get("is_promo_free")),
        "status": item["status"],
        "ready_at": item["ready_at"].isoformat() if item.get("ready_at") else None,
        "created_at": item["created_at"].isoformat() if item.get("created_at") else None,
    }


def _serialize_table_session_comanda(row: Any, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "id": str(row["id"]),
        "comanda_number": int(row["comanda_number"]),
        "comanda_index": int(row["comanda_index"]),
        "status": row["status"],
        "source_type": row["source_type"],
        "table_display_name": row["table_display_name"],
        "notes": row.get("notes"),
        "station_id": str(row["station_id"]) if row.get("station_id") else None,
        "station_name": row.get("station_name"),
        "station_kitchen_name": row.get("station_kitchen_name"),
        "station_color": row.get("station_color"),
        "fired_at": row["fired_at"].isoformat() if row.get("fired_at") else None,
        "preparing_at": row["preparing_at"].isoformat() if row.get("preparing_at") else None,
        "ready_at": row["ready_at"].isoformat() if row.get("ready_at") else None,
        "delivered_at": row["delivered_at"].isoformat() if row.get("delivered_at") else None,
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "items": items,
    }


async def get_table_session_comandas(request: Request, table_id: UUID) -> dict:
    """
    Get persisted printable comandas for the table's currently open session.
    Includes delivered tickets for reprint and excludes cancelled rows by default.
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
                SELECT id, opened_at
                FROM table_sessions
                WHERE table_id = $1
                  AND tenant_id = $2
                  AND closed_at IS NULL
                """,
                table_id,
                tenant_id,
            )
            if not session_row:
                raise NotFoundError("No open session for this table")

            rows = await conn.fetch(
                """
                SELECT
                    c.id,
                    c.comanda_number,
                    c.comanda_index,
                    c.status,
                    c.source_type,
                    c.table_display_name,
                    c.notes,
                    c.fired_at,
                    c.preparing_at,
                    c.ready_at,
                    c.delivered_at,
                    c.created_at,
                    ks.id AS station_id,
                    ks.name AS station_name,
                    ks.kitchen_name AS station_kitchen_name,
                    ks.color AS station_color
                FROM table_sessions ts
                JOIN orders o ON o.table_session_id = ts.id
                JOIN comandas c ON c.order_id = o.id
                JOIN kitchen_stations ks ON ks.id = c.station_id
                WHERE ts.id = $1
                  AND ts.table_id = $2
                  AND ts.tenant_id = $3
                  AND ts.closed_at IS NULL
                  AND o.tenant_id = $3
                  AND c.tenant_id = $3
                  AND c.status IN ('pending', 'preparing', 'ready', 'delivered')
                  AND (o.status IS NULL OR o.status != 'cancelled')
                ORDER BY c.fired_at ASC, c.comanda_number ASC, c.comanda_index ASC, c.id ASC
                """,
                session_row["id"],
                table_id,
                tenant_id,
            )

            comandas: List[Dict[str, Any]] = []
            for row in rows:
                item_rows = await conn.fetch(
                    """
                    SELECT id, order_item_id, kitchen_name, quantity, notes,
                           modifiers_snapshot, status, ready_at, created_at
                    FROM comanda_items
                    WHERE comanda_id = $1
                      AND status != 'cancelled'
                    ORDER BY created_at ASC, id ASC
                    """,
                    row["id"],
                )
                items = [_serialize_comanda_item(ir) for ir in item_rows]
                comandas.append(_serialize_table_session_comanda(row, items))

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
                    "opened_at": session_row["opened_at"].isoformat() if session_row.get("opened_at") else None,
                },
                "comandas": comandas,
            },
        }

    except (AuthenticationError, NotFoundError):
        raise
    except Exception as e:
        logger.error(f"Error fetching table session comandas for table {table_id}: {e}")
        raise APIError(f"Error fetching table comandas: {e}", status_code=500)


async def _fetch_tab_operation_context(
    conn, tenant_id: UUID, table_id: UUID
) -> Optional[Dict[str, Any]]:
    """Channel, session, table label, and effective waiter for bitácora events (#783)."""
    row = await conn.fetchrow(
        """
        SELECT
            t.name AS table_name,
            t.is_bar,
            ts.id AS table_session_id,
            COALESCE(ts.attended_by_member_id, t.assigned_member_id) AS effective_waiter_member_id
        FROM tables t
        JOIN table_sessions ts ON ts.table_id = t.id
        WHERE t.id = $1
          AND t.tenant_id = $2
          AND ts.closed_at IS NULL
        """,
        table_id,
        tenant_id,
    )
    if not row:
        return None
    return {
        "channel": "barra" if row["is_bar"] else "mesa",
        "table_name": row["table_name"],
        "table_session_id": row["table_session_id"],
        "effective_waiter_member_id": row["effective_waiter_member_id"],
    }


async def _prefetch_product_names(
    conn, tenant_id: UUID, product_ids: List[Any]
) -> Dict[str, str]:
    if not product_ids:
        return {}
    rows = await conn.fetch(
        """
        SELECT id, name FROM product
        WHERE tenant_id = $1 AND id = ANY($2::uuid[])
        """,
        tenant_id,
        list(product_ids),
    )
    return {str(r["id"]): r["name"] for r in rows}


async def _fetch_order_item_modifiers(conn, order_item_id: UUID) -> List[Dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT modifier_id, modifier_name, price_at_purchase, quantity,
               included_quantity_at_purchase
        FROM order_item_modifiers
        WHERE order_item_id = $1
        """,
        order_item_id,
    )
    return [
        {
            "id": str(r["modifier_id"]) if r["modifier_id"] else None,
            "name": r["modifier_name"],
            "price": float(r["price_at_purchase"]),
            "quantity": int(r["quantity"] or 1),
            "included_quantity": int(r["included_quantity_at_purchase"] or 0),
        }
        for r in rows
    ]


def _build_tab_item_payload(
    *,
    product_id: Any,
    product_name: Optional[str],
    quantity: Any,
    unit_price: Any,
    subtotal: Any,
    modifiers: List[Dict[str, Any]],
    notes: Optional[str],
    table_id: UUID,
    table_name: str,
    order_number: Any,
) -> Dict[str, Any]:
    return {
        "product_id": str(product_id) if product_id else None,
        "product_name": product_name,
        "quantity": float(quantity) if quantity is not None else None,
        "unit_price": float(unit_price),
        "subtotal": float(subtotal),
        "modifiers": modifiers,
        "notes": notes,
        "table_id": str(table_id),
        "table_name": table_name,
        "order_number": int(order_number) if order_number is not None else None,
    }


def _modifiers_from_request_item(item: dict) -> List[Dict[str, Any]]:
    return [
        {
            "id": m.get("id"),
            "name": m.get("name"),
            "price": float(m.get("price", 0)),
            "quantity": float(m.get("quantity", 1)),
        }
        for m in (item.get("modifiers") or [])
    ]


async def _record_tab_operation_event(
    conn,
    tenant_id: UUID,
    *,
    user_id: UUID,
    table_id: UUID,
    tab_ctx: Dict[str, Any],
    action: str,
    order_id: Optional[UUID] = None,
    order_item_id: Optional[UUID] = None,
    comanda_item_id: Optional[UUID] = None,
    payload: Optional[Dict[str, Any]] = None,
    reason: Optional[str] = None,
) -> None:
    await record_operation_event(
        conn,
        tenant_id,
        domain=DOMAIN_POS,
        channel=tab_ctx["channel"],
        action=action,
        actor_user_id=user_id,
        actor_member_id=tab_ctx.get("effective_waiter_member_id"),
        table_id=table_id,
        table_session_id=tab_ctx.get("table_session_id"),
        order_id=order_id,
        order_item_id=order_item_id,
        comanda_item_id=comanda_item_id,
        payload=payload,
        reason=reason,
    )


def _tab_item_requires_remove_reason(
    fulfillment_status: Optional[str],
    comanda_item_row: Optional[Any],
) -> bool:
    """Fired to kitchen: non-new fulfillment or linked comanda line (warocol.com#786)."""
    status = fulfillment_status or "new"
    if status not in ("new", "cancelled"):
        return True
    return comanda_item_row is not None


_TAB_ITEM_EDIT_BLOCKED_FULFILLMENT = frozenset({"preparing", "ready", "cancelled"})
_TAB_ITEM_EDIT_BLOCKED_COMANDA = frozenset({"preparing", "ready", "delivered", "cancelled"})

_FULFILLMENT_LABELS = {
    "new": "Sin enviar",
    "sent": "En cocina",
    "preparing": "Preparando",
    "ready": "Listo",
    "cancelled": "Cancelado",
}


def _tab_item_edit_block_reason(
    fulfillment_status: Optional[str],
    comanda_status: Optional[str] = None,
) -> Optional[str]:
    """Return human-readable block reason, or None if tab line content edit is allowed (#1151)."""
    status = fulfillment_status or "new"
    if status in _TAB_ITEM_EDIT_BLOCKED_FULFILLMENT:
        label = _FULFILLMENT_LABELS.get(status, status)
        return (
            f"La cocina ya aceptó este ítem (estado: {label}). "
            "No se pueden cambiar modificadores ni notas."
        )
    if comanda_status and comanda_status in _TAB_ITEM_EDIT_BLOCKED_COMANDA:
        return (
            "La cocina ya aceptó este ítem en comanda. "
            "No se pueden cambiar modificadores ni notas."
        )
    return None


async def _fetch_tab_item_comanda_context(conn, order_item_id: UUID) -> Optional[Any]:
    return await conn.fetchrow(
        """
        SELECT ci.id AS comanda_item_id, ci.status AS comanda_item_status,
               c.id AS comanda_id, c.status AS comanda_status
        FROM comanda_items ci
        JOIN comandas c ON c.id = ci.comanda_id
        WHERE ci.order_item_id = $1
          AND ci.status <> 'cancelled'
        ORDER BY c.created_at DESC
        LIMIT 1
        """,
        order_item_id,
    )


async def _fetch_open_tab_item_row(
    conn, order_item_id: UUID, table_id: UUID, tenant_id: UUID,
) -> Optional[Any]:
    return await conn.fetchrow(
        """
        SELECT
            oi.id, oi.product_id, oi.quantity, oi.price_at_purchase,
            oi.subtotal, oi.notes, oi.fulfillment_status,
            o.id AS order_id, o.total_amount, o.order_number,
            p.name AS product_name,
            t.name AS table_name,
            t.is_bar,
            ts.id AS table_session_id,
            COALESCE(ts.attended_by_member_id, t.assigned_member_id)
                AS effective_waiter_member_id
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        JOIN table_sessions ts ON ts.id = o.table_session_id
        JOIN tables t ON t.id = ts.table_id
        JOIN product p ON p.id = oi.product_id
        WHERE oi.id = $1
          AND ts.table_id = $2
          AND ts.tenant_id = $3
          AND ts.closed_at IS NULL
        """,
        order_item_id, table_id, tenant_id,
    )


async def get_tab_item_edit_eligibility(
    request: Request,
    table_id: UUID,
    order_item_id: UUID,
    *,
    record_attempt: bool = False,
) -> dict:
    """Check whether tab line content edit is allowed; optionally audit blocked attempts."""
    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id
    user_id = session_context.user_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    async with get_db_connection(use_transaction=True) as conn:
        row = await _fetch_open_tab_item_row(conn, order_item_id, table_id, tenant_id)
        if not row:
            raise NotFoundError("Order item not found or session already closed")

        comanda_ctx = await _fetch_tab_item_comanda_context(conn, order_item_id)
        comanda_status = comanda_ctx["comanda_status"] if comanda_ctx else None
        block_reason = _tab_item_edit_block_reason(row["fulfillment_status"], comanda_status)
        fulfillment_status = row["fulfillment_status"] or "new"

        if block_reason and record_attempt:
            tab_ctx = {
                "channel": "barra" if row["is_bar"] else "mesa",
                "table_name": row["table_name"],
                "table_session_id": row["table_session_id"],
                "effective_waiter_member_id": row["effective_waiter_member_id"],
            }
            await _record_tab_operation_event(
                conn,
                tenant_id,
                user_id=user_id,
                table_id=table_id,
                tab_ctx=tab_ctx,
                action="tab_item_edit_blocked",
                order_id=row["order_id"],
                order_item_id=order_item_id,
                comanda_item_id=comanda_ctx["comanda_item_id"] if comanda_ctx else None,
                payload={
                    "product_name": row["product_name"],
                    "fulfillment_status": fulfillment_status,
                    "comanda_status": comanda_status,
                },
            )

        if block_reason:
            raise APIError(
                block_reason,
                status_code=409,
                details={
                    "code": "TAB_ITEM_EDIT_KITCHEN_ACCEPTED",
                    "fulfillment_status": fulfillment_status,
                    "comanda_status": comanda_status,
                },
            )

        return {
            "success": True,
            "data": {
                "allowed": True,
                "fulfillment_status": fulfillment_status,
                "comanda_status": comanda_status,
            },
        }


async def update_tab_item_content(
    request: Request,
    table_id: UUID,
    order_item_id: UUID,
    modifiers: List[dict],
    notes: Optional[str],
) -> dict:
    """Replace modifiers and notes on a tab line when kitchen has not accepted yet (#1151)."""
    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id
    user_id = session_context.user_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    async with get_db_connection(use_transaction=True) as conn:
        row = await _fetch_open_tab_item_row(conn, order_item_id, table_id, tenant_id)
        if not row:
            raise NotFoundError("Order item not found or session already closed")

        comanda_ctx = await _fetch_tab_item_comanda_context(conn, order_item_id)
        comanda_status = comanda_ctx["comanda_status"] if comanda_ctx else None
        block_reason = _tab_item_edit_block_reason(row["fulfillment_status"], comanda_status)
        if block_reason:
            raise APIError(
                block_reason,
                status_code=409,
                details={
                    "code": "TAB_ITEM_EDIT_KITCHEN_ACCEPTED",
                    "fulfillment_status": row["fulfillment_status"] or "new",
                    "comanda_status": comanda_status,
                },
            )

        old_modifiers = await _fetch_order_item_modifiers(conn, order_item_id)
        old_notes = row["notes"]
        quantity = float(row["quantity"])
        modifiers = await resolve_modifier_selections(
            conn, row["product_id"], modifiers or []
        )

        await conn.execute(
            "DELETE FROM order_item_modifiers WHERE order_item_id = $1",
            order_item_id,
        )
        modifier_sum = 0.0
        for mod in modifiers or []:
            mod_qty = int(mod.get("quantity") or 1)
            mod_price = float(mod.get("price") or 0)
            included_quantity = int(mod.get("included_quantity") or 0)
            modifier_sum += _modifier_unit_total(mod)
            mod_id = mod.get("id")
            await conn.execute(
                """
                INSERT INTO order_item_modifiers (
                    order_item_id, modifier_id, modifier_name, price_at_purchase,
                    quantity, included_quantity_at_purchase
                )
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                order_item_id,
                mod_id,
                mod.get("name"),
                mod_price,
                mod_qty,
                included_quantity,
            )

        normalized_notes = (notes or "").strip() or None
        effective_unit_price = float(row["price_at_purchase"]) + modifier_sum
        new_subtotal = effective_unit_price * quantity
        old_subtotal = float(row["subtotal"])

        await conn.execute(
            """
            UPDATE order_items
            SET notes = $1, subtotal = $2
            WHERE id = $3
            """,
            normalized_notes,
            new_subtotal,
            order_item_id,
        )
        new_total = max(0.0, float(row["total_amount"]) - old_subtotal + new_subtotal)
        await conn.execute(
            "UPDATE orders SET total_amount = $1 WHERE id = $2",
            new_total,
            row["order_id"],
        )

        if comanda_ctx and comanda_status == "pending":
            snapshot = [
                {
                    "name": m.get("name"),
                    "price": float(m.get("price") or 0),
                    "quantity": int(m.get("quantity") or 1),
                    "included_quantity": int(m.get("included_quantity") or 0),
                }
                for m in (modifiers or [])
            ]
            await conn.execute(
                """
                UPDATE comanda_items
                SET modifiers_snapshot = $1::jsonb, notes = $2
                WHERE id = $3
                """,
                json.dumps(snapshot) if snapshot else None,
                normalized_notes,
                comanda_ctx["comanda_item_id"],
            )

        new_modifiers = await _fetch_order_item_modifiers(conn, order_item_id)
        tab_ctx = {
            "channel": "barra" if row["is_bar"] else "mesa",
            "table_name": row["table_name"],
            "table_session_id": row["table_session_id"],
            "effective_waiter_member_id": row["effective_waiter_member_id"],
        }
        payload = _build_tab_item_payload(
            product_id=row["product_id"],
            product_name=row["product_name"],
            quantity=quantity,
            unit_price=row["price_at_purchase"],
            subtotal=new_subtotal,
            modifiers=new_modifiers,
            notes=normalized_notes,
            table_id=table_id,
            table_name=row["table_name"],
            order_number=row["order_number"],
        )
        payload["previous_modifiers"] = old_modifiers
        payload["previous_notes"] = old_notes

        await _record_tab_operation_event(
            conn,
            tenant_id,
            user_id=user_id,
            table_id=table_id,
            tab_ctx=tab_ctx,
            action="tab_item_edited",
            order_id=row["order_id"],
            order_item_id=order_item_id,
            comanda_item_id=comanda_ctx["comanda_item_id"] if comanda_ctx else None,
            payload=payload,
        )

        return {
            "success": True,
            "data": {
                "order_item_id": str(order_item_id),
                "subtotal": new_subtotal,
                "notes": normalized_notes,
                "modifiers": new_modifiers,
            },
        }


def _normalize_audit_reason(reason: Optional[str]) -> Optional[str]:
    normalized = (reason or "").strip()
    return normalized or None


async def _record_tab_cleared_pending_lines(
    conn,
    tenant_id: UUID,
    *,
    user_id: UUID,
    table_id: UUID,
    session_id: UUID,
    reason: Optional[str],
) -> int:
    """Emit tab_cleared for each pending order line (bitácora). Returns line count."""
    tab_ctx = await _fetch_tab_operation_context(conn, tenant_id, table_id)
    if not tab_ctx:
        return 0

    pending_lines = await conn.fetch(
        """
        SELECT
            oi.id AS order_item_id,
            oi.product_id,
            oi.quantity,
            oi.price_at_purchase,
            oi.subtotal,
            oi.notes,
            o.id AS order_id,
            o.order_number,
            p.name AS product_name
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        JOIN product p ON p.id = oi.product_id
        WHERE o.table_session_id = $1 AND o.status = 'pending'
        """,
        session_id,
    )
    event_reason = _normalize_audit_reason(reason)
    for line in pending_lines:
        line_modifiers = await _fetch_order_item_modifiers(conn, line["order_item_id"])
        await _record_tab_operation_event(
            conn,
            tenant_id,
            user_id=user_id,
            table_id=table_id,
            tab_ctx=tab_ctx,
            action="tab_cleared",
            order_id=line["order_id"],
            order_item_id=line["order_item_id"],
            reason=event_reason,
            payload=_build_tab_item_payload(
                product_id=line["product_id"],
                product_name=line["product_name"],
                quantity=line["quantity"],
                unit_price=line["price_at_purchase"],
                subtotal=line["subtotal"],
                modifiers=line_modifiers,
                notes=line["notes"],
                table_id=table_id,
                table_name=tab_ctx["table_name"],
                order_number=line["order_number"],
            ),
        )
    return len(pending_lines)


async def _restore_pending_session_orders_inventory(
    conn,
    *,
    tenant_id: UUID,
    user_id: UUID,
    session_id: UUID,
) -> None:
    """Restore stock for pending session orders that already consumed (warocol.com#2567).

    Fail-closed: any restore error propagates so discard/clear do not DELETE unrestored stock.
    """
    pending_orders = await conn.fetch(
        """
        SELECT id, order_number
        FROM orders
        WHERE table_session_id = $1
          AND tenant_id = $2
          AND status = 'pending'
        """,
        session_id,
        tenant_id,
    )
    for ord_row in pending_orders:
        await _return_stock_for_order_cancellation(
            conn,
            ord_row["id"],
            tenant_id,
            user_id,
            int(ord_row["order_number"]),
        )


async def _order_has_consumption_movements(conn, *, tenant_id: UUID, order_id: UUID) -> bool:
    return bool(await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1
            FROM tenant_ingredient_movements
            WHERE tenant_id = $1
              AND reference_table = 'orders'
              AND reference_id = $2
              AND movement_type = 'consumption'
              AND quantity_change < 0
        )
        """,
        tenant_id,
        order_id,
    ))


async def _ensure_tab_order_inventory_at_close(
    conn,
    *,
    tenant_id: UUID,
    user_id: UUID,
    order_row,
) -> None:
    """Deduct recipe + modifier stock at mesa close when send did not (flag off)."""
    order_id = order_row["id"]
    order_number = int(order_row["order_number"])
    already = await _order_inventory_already_consumed_before_completion(
        conn,
        row=order_row,
        order_id=order_id,
        tenant_id=tenant_id,
        old_status="pending",
    )
    if already:
        return

    await _deduct_stock_for_status_update(conn, order_id, tenant_id, user_id, order_number)

    items = await conn.fetch(
        """
        SELECT id, product_id, quantity
        FROM order_items
        WHERE order_id = $1
        """,
        order_id,
    )
    for item in items:
        mods = await conn.fetch(
            """
            SELECT modifier_id, modifier_name, quantity
            FROM order_item_modifiers
            WHERE order_item_id = $1
            """,
            item["id"],
        )
        for mod in mods:
            if not mod["modifier_id"]:
                continue
            await _deduct_modifier_inventory_for_order_item(
                conn,
                tenant_id=tenant_id,
                user_id=user_id,
                order_id=order_id,
                order_item_id=item["id"],
                order_number=order_number,
                item_quantity=float(item["quantity"]),
                modifier={
                    "id": str(mod["modifier_id"]),
                    "name": mod["modifier_name"],
                },
                modifier_qty=float(mod["quantity"] or 1),
            )


async def _return_tab_item_inventory_from_snapshots(
    conn,
    *,
    tenant_id: UUID,
    user_id: UUID,
    order_id: UUID,
    order_number: int,
    order_item_id: UUID,
    product_name: str,
) -> None:
    """Return stock captured in order_item_ingredients when a tab line is removed."""
    snapshots = await conn.fetch(
        """
        SELECT ingredient_id, ingredient_name, quantity, unit
        FROM order_item_ingredients
        WHERE order_item_id = $1
        """,
        order_item_id,
    )
    for snap in snapshots:
        qty = float(snap["quantity"] or 0)
        if qty <= 0:
            continue
        await _return_ingredient_to_stock(
            conn,
            tenant_id,
            user_id,
            order_id,
            order_number,
            snap["ingredient_id"],
            qty,
            snap["unit"] or "und",
            snap["ingredient_name"],
            f"Devolución tab: {product_name}",
        )


async def remove_tab_item(
    request: Request,
    table_id: UUID,
    order_item_id: UUID,
    reason: Optional[str] = None,
) -> dict:
    """Remove an order item from the running tab and update the order total."""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection(use_transaction=True) as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    oi.id, oi.product_id, oi.quantity, oi.price_at_purchase,
                    oi.subtotal, oi.notes, oi.fulfillment_status,
                    o.id AS order_id, o.total_amount, o.order_number, o.table_session_id,
                    p.name AS product_name,
                    t.name AS table_name,
                    t.is_bar,
                    COALESCE(ts.attended_by_member_id, t.assigned_member_id)
                        AS effective_waiter_member_id
                FROM order_items oi
                JOIN orders o ON o.id = oi.order_id
                JOIN table_sessions ts ON ts.id = o.table_session_id
                JOIN tables t ON t.id = ts.table_id
                JOIN product p ON p.id = oi.product_id
                WHERE oi.id = $1
                  AND ts.table_id = $2
                  AND ts.tenant_id = $3
                  AND ts.closed_at IS NULL
                """,
                order_item_id, table_id, tenant_id,
            )
            if not row:
                raise NotFoundError("Order item not found or session already closed")

            tab_ctx = {
                "channel": "barra" if row["is_bar"] else "mesa",
                "table_name": row["table_name"],
                "table_session_id": row["table_session_id"],
                "effective_waiter_member_id": row["effective_waiter_member_id"],
            }
            modifiers = await _fetch_order_item_modifiers(conn, order_item_id)

            # Cancel linked comanda_item BEFORE deleting order_item (FK: no cascade).
            # If comandas are disabled no row will be found — no-op, continues as before.
            comanda_item_row = await conn.fetchrow(
                "SELECT id, comanda_id FROM comanda_items WHERE order_item_id = $1",
                order_item_id,
            )

            normalized_reason = (reason or "").strip()
            if _tab_item_requires_remove_reason(row["fulfillment_status"], comanda_item_row):
                if not normalized_reason:
                    raise APIError(
                        "Motivo requerido para eliminar un producto enviado a cocina",
                        status_code=400,
                    )
            event_reason = normalized_reason or None
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

            comanda_item_id = (
                comanda_item_row["id"] if comanda_item_row else None
            )
            await _record_tab_operation_event(
                conn,
                tenant_id,
                user_id=user_id,
                table_id=table_id,
                tab_ctx=tab_ctx,
                action="tab_item_removed",
                order_id=row["order_id"],
                order_item_id=order_item_id,
                comanda_item_id=comanda_item_id,
                reason=event_reason,
                payload=_build_tab_item_payload(
                    product_id=row["product_id"],
                    product_name=row["product_name"],
                    quantity=row["quantity"],
                    unit_price=row["price_at_purchase"],
                    subtotal=row["subtotal"],
                    modifiers=modifiers,
                    notes=row["notes"],
                    table_id=table_id,
                    table_name=row["table_name"],
                    order_number=row["order_number"],
                ),
            )

            try:
                # warocol.com#2566 — only restore qty that was actually deducted on send
                if await _order_has_consumption_movements(
                    conn, tenant_id=tenant_id, order_id=row["order_id"]
                ):
                    await _return_tab_item_inventory_from_snapshots(
                        conn,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        order_id=row["order_id"],
                        order_number=row["order_number"],
                        order_item_id=order_item_id,
                        product_name=row["product_name"],
                    )
            except Exception as _ret_exc:
                logger.error(
                    f"[tab] inventory return failed for item {order_item_id}: {_ret_exc}"
                )

            await conn.execute("DELETE FROM order_items WHERE id = $1", order_item_id)
            new_total = max(0.0, float(row["total_amount"]) - float(row["subtotal"]))
            await conn.execute(
                "UPDATE orders SET total_amount = $1 WHERE id = $2",
                new_total, row["order_id"],
            )

            # Re-evaluate promos for the remaining tab lines: the arithmetic
            # adjustment above ignores promo savings, so without this the locked
            # savings go stale when the eligible pool shrinks (#665).
            from app.services.promotions_service import persist_session_tab_promos

            await persist_session_tab_promos(
                conn, UUID(str(tenant_id)), row["table_session_id"]
            )

        return {"success": True, "data": {"removed": str(order_item_id)}}
    except (AuthenticationError, NotFoundError, APIError):
        raise
    except Exception as e:
        logger.error(f"Error removing tab item {order_item_id}: {e}")
        raise APIError(f"Error removing item: {e}", status_code=500)


async def update_tab_item_quantity(
    request: Request, table_id: UUID, order_item_id: UUID, quantity: int, reason: Optional[str] = None
) -> dict:
    """Update quantity of an order item in the running tab."""
    if quantity < 1:
        raise APIError("Quantity must be at least 1", status_code=400)
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection(use_transaction=True) as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    oi.id, oi.product_id, oi.quantity, oi.price_at_purchase,
                    oi.subtotal, oi.notes, oi.fulfillment_status,
                    o.id AS order_id, o.total_amount, o.order_number,
                    p.name AS product_name,
                    t.name AS table_name,
                    t.is_bar,
                    ts.id AS table_session_id,
                    COALESCE(ts.attended_by_member_id, t.assigned_member_id)
                        AS effective_waiter_member_id
                FROM order_items oi
                JOIN orders o ON o.id = oi.order_id
                JOIN table_sessions ts ON ts.id = o.table_session_id
                JOIN tables t ON t.id = ts.table_id
                JOIN product p ON p.id = oi.product_id
                WHERE oi.id = $1
                  AND ts.table_id = $2
                  AND ts.tenant_id = $3
                  AND ts.closed_at IS NULL
                """,
                order_item_id, table_id, tenant_id,
            )
            if not row:
                raise NotFoundError("Order item not found or session already closed")

            old_quantity = float(row["quantity"])
            comanda_item_row = await conn.fetchrow(
                "SELECT id, comanda_id FROM comanda_items WHERE order_item_id = $1",
                order_item_id,
            )
            normalized_reason = _normalize_audit_reason(reason)
            if quantity < old_quantity and _tab_item_requires_remove_reason(
                row["fulfillment_status"], comanda_item_row
            ):
                if not normalized_reason:
                    raise APIError(
                        "Motivo requerido para reducir cantidad de un producto enviado a cocina",
                        status_code=400,
                    )
            event_reason = normalized_reason or None
            tab_ctx = {
                "channel": "barra" if row["is_bar"] else "mesa",
                "table_name": row["table_name"],
                "table_session_id": row["table_session_id"],
                "effective_waiter_member_id": row["effective_waiter_member_id"],
            }
            modifiers = await _fetch_order_item_modifiers(conn, order_item_id)

            modifier_sum = await conn.fetchval(
                """
                SELECT COALESCE(SUM(
                    price_at_purchase
                    * GREATEST(
                        COALESCE(quantity, 1) - included_quantity_at_purchase,
                        0
                    )
                ), 0)
                FROM order_item_modifiers
                WHERE order_item_id = $1
                """,
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

            # Re-evaluate promos after the quantity change: the arithmetic
            # adjustment above ignores promo savings, so without this the locked
            # savings go stale when the eligible pool grows or shrinks (#665).
            from app.services.promotions_service import persist_session_tab_promos

            await persist_session_tab_promos(
                conn, UUID(str(tenant_id)), row["table_session_id"]
            )

            payload = _build_tab_item_payload(
                product_id=row["product_id"],
                product_name=row["product_name"],
                quantity=quantity,
                unit_price=row["price_at_purchase"],
                subtotal=new_subtotal,
                modifiers=modifiers,
                notes=row["notes"],
                table_id=table_id,
                table_name=row["table_name"],
                order_number=row["order_number"],
            )
            payload["old_quantity"] = old_quantity
            payload["new_quantity"] = float(quantity)

            await _record_tab_operation_event(
                conn,
                tenant_id,
                user_id=user_id,
                table_id=table_id,
                tab_ctx=tab_ctx,
                action="tab_item_qty_changed",
                order_id=row["order_id"],
                order_item_id=order_item_id,
                payload=payload,
                reason=event_reason,
            )

        return {"success": True, "data": {"order_item_id": str(order_item_id), "quantity": quantity, "subtotal": new_subtotal}}
    except (AuthenticationError, NotFoundError, APIError):
        raise
    except Exception as e:
        logger.error(f"Error updating tab item {order_item_id}: {e}")
        raise APIError(f"Error updating item: {e}", status_code=500)


async def update_tab_item_promo_opt_out(
    request: Request,
    table_id: UUID,
    order_item_id: UUID,
    promo_opt_out: bool,
) -> dict:
    """Toggle per-line promotion opt-out for a mesa/tab order item (warocol.com#1003)."""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection(use_transaction=True) as conn:
            enabled = await conn.fetchval(
                """
                SELECT allow_promo_line_opt_out
                FROM tenant_public_profiles
                WHERE tenant_id = $1
                """,
                tenant_id,
            )
            if not bool(enabled):
                raise APIError(
                    "Per-line promotion opt-out is not enabled for this tenant",
                    status_code=403,
                )

            row = await conn.fetchrow(
                """
                SELECT oi.id, ts.id AS session_id
                FROM order_items oi
                JOIN orders o ON o.id = oi.order_id
                JOIN table_sessions ts ON ts.id = o.table_session_id
                WHERE oi.id = $1
                  AND ts.table_id = $2
                  AND ts.tenant_id = $3
                  AND ts.closed_at IS NULL
                  AND o.status = 'pending'
                """,
                order_item_id,
                table_id,
                tenant_id,
            )
            if not row:
                raise NotFoundError("Order item not found or session already closed")

            await conn.execute(
                "UPDATE order_items SET promo_opt_out = $1 WHERE id = $2",
                promo_opt_out,
                order_item_id,
            )

            from app.services.promotions_service import persist_session_tab_promos

            await persist_session_tab_promos(conn, UUID(str(tenant_id)), row["session_id"])

        return {
            "success": True,
            "data": {
                "order_item_id": str(order_item_id),
                "promo_opt_out": promo_opt_out,
            },
        }
    except (AuthenticationError, NotFoundError, APIError):
        raise
    except Exception as e:
        logger.error(f"Error updating tab item promo opt-out {order_item_id}: {e}")
        raise APIError(f"Error updating promo opt-out: {e}", status_code=500)


async def _add_tab_items_core(
    conn,
    tenant_id: UUID,
    user_id: UUID,
    table_id: UUID,
    items: List[dict],
) -> dict:
    """
    Add tab items using an existing connection (caller owns transaction).
    Used by add_tab_items and table QR accept (#268).
    """
    session_row = await conn.fetchrow(
        """
        SELECT
            ts.id AS session_id,
            t.name AS table_name,
            t.is_bar,
            COALESCE(ts.attended_by_member_id, t.assigned_member_id)
                AS effective_waiter_member_id
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

    deduct_flag = False
    try:
        deduct_flag = await conn.fetchval(
            """
            SELECT deduct_inventory_on_command
            FROM tenant_public_profiles
            WHERE tenant_id = $1
            """,
            tenant_id,
        )
    except Exception as _flag_exc:
        # Column missing until migration — opt-in off (warocol.com#2572).
        if "deduct_inventory_on_command" not in str(_flag_exc):
            raise
        logger.warning(
            "[tab] deduct_inventory_on_command missing; defaulting false until migration"
        )
        deduct_flag = False
    deduct_on_command = False if deduct_flag is None else bool(deduct_flag)

    session_id = session_row["session_id"]
    tab_ctx = {
        "channel": "barra" if session_row["is_bar"] else "mesa",
        "table_name": session_row["table_name"],
        "table_session_id": session_id,
        "effective_waiter_member_id": session_row["effective_waiter_member_id"],
    }
    product_ids = list({item["product_id"] for item in items})
    product_names = await _prefetch_product_names(conn, tenant_id, product_ids)
    pricing_map = await fetch_product_pricing_map(conn, tenant_id, product_ids)
    validate_items_unit_prices(pricing_map, items)
    for item in items:
        item["modifiers"] = await resolve_modifier_selections(
            conn,
            UUID(str(item["product_id"])),
            item.get("modifiers") or [],
        )

    for item in items:
        mod_sum = sum(_modifier_unit_total(m) for m in (item.get("modifiers") or []))
        logger.info(
            f"[add_tab_items] item product_id={item['product_id']} "
            f"qty={item['quantity']} unit_price={item['unit_price']} "
            f"modifier_sum={mod_sum} "
            f"line_total={item['quantity'] * (item['unit_price'] + mod_sum)}"
        )
    batch_amount = sum(
        item["quantity"] * (
            item["unit_price"]
            + sum(_modifier_unit_total(m) for m in (item.get("modifiers") or []))
        )
        for item in items
    )
    logger.info(f"[add_tab_items] batch_amount={batch_amount} items_count={len(items)}")

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

    created_order_item_ids: List[UUID] = []
    for item in items:
        modifier_unit_total = sum(_modifier_unit_total(m) for m in (item.get("modifiers") or []))
        subtotal = item["quantity"] * (item["unit_price"] + modifier_unit_total)
        item_notes = (item.get("notes") or "").strip() or None
        order_item_row = await conn.fetchrow(
            """
            INSERT INTO order_items (
                order_id, product_id, quantity,
                price_at_purchase, subtotal, notes
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
            """,
            order_id,
            item["product_id"],
            item["quantity"],
            item["unit_price"],
            subtotal,
            item_notes,
        )
        order_item_id = order_item_row["id"]
        created_order_item_ids.append(order_item_id)

        for mod in item.get("modifiers") or []:
            modifier_qty = mod.get("quantity", 1)
            await conn.execute(
                """
                INSERT INTO order_item_modifiers (
                    order_item_id, modifier_id, modifier_name,
                    price_at_purchase, quantity, included_quantity_at_purchase
                )
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                order_item_id,
                mod.get("id"),
                mod.get("name"),
                mod.get("price", 0),
                modifier_qty,
                mod.get("included_quantity", 0),
            )

        try:
            if deduct_on_command:
                for mod in item.get("modifiers") or []:
                    modifier_qty = float(mod.get("quantity", 1))
                    await _deduct_modifier_inventory_for_order_item(
                        conn,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        order_id=order_id,
                        order_item_id=order_item_id,
                        order_number=order_number,
                        item_quantity=float(item["quantity"]),
                        modifier=mod,
                        modifier_qty=modifier_qty,
                    )
        except Exception as _mod_inv_exc:
            logger.error(
                f"[tab] modifier inventory deduction failed for item {order_item_id}: {_mod_inv_exc}"
            )

        try:
            await _capture_order_item_ingredients(
                conn, order_item_id, item["product_id"],
                float(item["quantity"]), str(tenant_id)
            )
        except Exception as _snap_exc:
            logger.error(f"[tab] ingredient snapshot failed for item {order_item_id}: {_snap_exc}")

        if not deduct_on_command:
            continue

        try:
            ingredients = await conn.fetch(
                """
                SELECT pr.ingredient_id, pr.quantity, pr.unit, i.name as ingredient_name
                FROM product_recipes pr
                JOIN ingredients i ON i.id = pr.ingredient_id
                WHERE pr.product_id = $1
                UNION ALL
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

    from app.services.promotions_service import persist_session_tab_promos

    promo_eval = await persist_session_tab_promos(conn, tenant_id, session_id)
    refreshed_order = await conn.fetchrow(
        "SELECT total_amount FROM orders WHERE id = $1",
        order_id,
    )
    if refreshed_order:
        order_total = float(refreshed_order["total_amount"])

    return {
        "order_id": order_id,
        "order_number": order_number,
        "total_amount": order_total,
        "session_id": session_id,
        "items_count": len(items),
        "created_order_item_ids": created_order_item_ids,
        "promo_savings": float(promo_eval.get("promo_savings") or 0),
        "promo_breakdown": promo_eval.get("promo_breakdown") or [],
        "promo_lines": promo_eval.get("lines") or [],
    }


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

        tab_result: dict = {}
        async with get_db_connection() as conn:
            async with conn.transaction():
                tab_result = await _add_tab_items_core(conn, tenant_id, user_id, table_id, items)

        order_id = tab_result["order_id"]
        order_number = tab_result["order_number"]
        order_total = tab_result["total_amount"]
        promo_savings = tab_result.get("promo_savings", 0)
        promo_breakdown = tab_result.get("promo_breakdown") or []
        promo_lines = tab_result.get("promo_lines") or []
        created_order_item_ids = tab_result.get("created_order_item_ids") or []

        # Auto-fire comandas if enabled (#753 — return comandas for POS print)
        fired_comandas: List[dict] = []
        fired_items_count = 0
        try:
            fire_result = await fire_table_items(
                request,
                table_id,
                item_ids=created_order_item_ids,
            )
            if fire_result and isinstance(fire_result.get("data"), dict):
                fired_comandas = fire_result["data"].get("comandas") or []
                fired_items_count = int(fire_result["data"].get("fired_items_count") or 0)
            else:
                fired_comandas = fire_result.get("comandas") or []
                fired_items_count = int(fire_result.get("fired_items_count") or 0)
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
                "promo_savings": promo_savings,
                "promo_breakdown": promo_breakdown,
                "lines": promo_lines,
                "comandas": fired_comandas,
                "fired_items_count": fired_items_count,
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
        user_id = session_context.user_id
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

                # warocol.com#2567 — restore stock before hard-delete when commanded early
                await _restore_pending_session_orders_inventory(
                    conn,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    session_id=session_id,
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
                    """
                    SELECT
                        id,
                        minimum_consumption_enabled_snapshot,
                        minimum_consumption_amount_snapshot,
                        minimum_consumption_restrictive_snapshot,
                        covers,
                        custom_label
                    FROM table_sessions
                    WHERE table_id = $1 AND tenant_id = $2 AND closed_at IS NULL
                    LIMIT 1
                    """,
                    source_table_id, tenant_id,
                )
                if not source_session:
                    raise NotFoundError("source table has no open session")

                # 3. Lock + validate target table
                target = await conn.fetchrow(
                    "SELECT id, name, status, is_bar, capacity FROM tables "
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
                _covers, _cap_snap = guest_snapshot_from_capacity(target["capacity"])
                if source_session["covers"] is not None:
                    _covers = int(source_session["covers"])
                new_session = await conn.fetchrow(
                    """
                    INSERT INTO table_sessions (
                        table_id,
                        tenant_id,
                        opened_by_user_id,
                        minimum_consumption_enabled_snapshot,
                        minimum_consumption_amount_snapshot,
                        minimum_consumption_restrictive_snapshot,
                        covers,
                        capacity_snapshot,
                        custom_label
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    RETURNING id
                    """,
                    target_table_id,
                    tenant_id,
                    user_id,
                    source_session["minimum_consumption_enabled_snapshot"],
                    source_session["minimum_consumption_amount_snapshot"],
                    source_session["minimum_consumption_restrictive_snapshot"],
                    _covers,
                    _cap_snap,
                    source_session["custom_label"],
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
                    if not table["qr_enabled"]:
                        await check_plan_quota_growth(conn, tenant_id, "active_qr_tables")
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
                table = await _get_table_for_qr_management(conn, tenant_id, table_id)
                if not table["qr_enabled"]:
                    await check_plan_quota_growth(conn, tenant_id, "active_qr_tables")
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
        "code": row.get("code"),
        "capacity": row["capacity"],
        "display_order": row.get("display_order"),
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
            "minimum_consumption": _minimum_consumption_state(
                row,
                float(row.get("paid_total") or 0),
                float(row.get("active_advance_total_cop") or 0),
            ),
            # Session-level waiter override (warocol.com#574)
            "attended_by_member_id": str(row["session_attended_by_member_id"]) if row.get("session_attended_by_member_id") else None,
            "attended_by_member_name": row.get("session_attended_by_member_name"),
            "attended_by_member_role": row.get("session_attended_by_member_role"),
            "custom_label": row.get("session_custom_label"),
            "covers": int(row["session_covers"]) if row.get("session_covers") is not None else None,
            "capacity_snapshot": int(row["session_capacity_snapshot"]) if row.get("session_capacity_snapshot") is not None else None,
        }
    return result


async def clear_tab(request: Request, table_id: UUID, reason: Optional[str] = None) -> dict:
    """
    Delete all pending orders (and their items) linked to the active session of a table.
    Does NOT close the session — the table stays open and ready for new orders.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id
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

                pending_line_count = await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM order_items oi
                    JOIN orders o ON o.id = oi.order_id
                    WHERE o.table_session_id = $1 AND o.status = 'pending'
                    """,
                    session_row["id"],
                )
                if pending_line_count and pending_line_count > 0:
                    if not _normalize_audit_reason(reason):
                        raise APIError(
                            "Motivo requerido para vaciar la cuenta",
                            status_code=400,
                        )
                    await _record_tab_cleared_pending_lines(
                        conn,
                        tenant_id,
                        user_id=user_id,
                        table_id=table_id,
                        session_id=session_row["id"],
                        reason=reason,
                    )

                # warocol.com#2567 — restore stock before deleting pending lines
                await _restore_pending_session_orders_inventory(
                    conn,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    session_id=session_row["id"],
                )

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

                # Cancel parent comandas so KDS does not show orphaned tickets (#139, #799)
                await conn.execute(
                    """
                    UPDATE comandas
                    SET status = 'cancelled', updated_at = NOW()
                    WHERE order_id IN (
                        SELECT id FROM orders
                        WHERE table_session_id = $1 AND status = 'pending'
                    )
                      AND tenant_id = $2
                      AND status IN ('pending', 'preparing', 'ready')
                    """,
                    session_row["id"],
                    tenant_id,
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


async def fire_table_items(
    request: Request,
    table_id: UUID,
    item_ids: Optional[List[UUID]] = None,
) -> dict:
    """
    Explicitly fire 'new' items in the current table session to the KDS.
    When item_ids is set, only those order_items are fired (#753).
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
                        item_ids=item_ids,
                        conn=conn,
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
        "code": row.get("code"),
        "capacity": row["capacity"],
        "display_order": row.get("display_order"),
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

                table_meta = await conn.fetchrow(
                    "SELECT is_bar FROM tables WHERE id = $1 AND tenant_id = $2",
                    table_id, tenant_id,
                )
                mesa_channel = "barra" if table_meta and table_meta["is_bar"] else "mesa"

                # 4. Find sibling rows (proportional split): same session,
                # same method, same paid_at, not voided. Lock them all.
                sibling_rows = await conn.fetch(
                    """
                    SELECT op.id, op.order_id, op.amount, o.customer_id
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
                    "UPDATE order_payments SET voided_at = NOW(), void_reason = $2 WHERE id = ANY($1::uuid[])",
                    [r["id"] for r in sibling_rows],
                    normalized_reason,
                )

                # 5b. Restore wallet for each voided wallet tender portion (#2020).
                if target["payment_method"] == "customer_wallet":
                    from app.services.customer_wallet_service import (
                        restore_wallet_for_order_payment_void,
                    )
                    from decimal import Decimal as _Dec

                    for sib in sibling_rows:
                        if not sib["customer_id"] or float(sib["amount"] or 0) <= 0:
                            continue
                        await restore_wallet_for_order_payment_void(
                            conn,
                            UUID(str(sib["customer_id"])),
                            UUID(str(tenant_id)),
                            _Dec(str(sib["amount"])),
                            sib["order_id"],
                            UUID(str(sib["id"])),
                            UUID(str(user_id)) if user_id else None,
                            notes=f"Anulación pago mesa: {normalized_reason}",
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
                
                session_tip_row = await conn.fetchrow(
                    """
                    SELECT COALESCE(tip_amount, 0) AS tip_amount,
                           COALESCE(tip_tax_amount, 0) AS tip_tax_amount
                    FROM orders
                    WHERE table_session_id = $1 AND status = 'completed'
                    ORDER BY created_at LIMIT 1
                    """,
                    session_row["id"],
                )
                amount_due = split_settlement_amount_due(
                    session_total,
                    float(session_tip_row["tip_amount"] or 0) if session_tip_row else 0.0,
                    float(session_tip_row["tip_tax_amount"] or 0) if session_tip_row else 0.0,
                )
                remaining = max(0.0, amount_due - paid_total)
                is_complete = remaining <= 0.01

                # 7. Reopen if voiding flipped the session out of fully settled.
                # Credit splits close with payment_status partial/credit (#2020).
                from app.services.credit_service import sync_order_split_credit_status
                was_closed = session_row["closed_at"] is not None
                reopened = was_closed and not is_complete
                for oid in order_ids:
                    await sync_order_split_credit_status(
                        conn, oid, settlement_complete=is_complete and not reopened,
                    )
                if reopened:
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

                void_amount = sum(float(r["amount"]) for r in sibling_rows)
                await record_operation_event(
                    conn,
                    tenant_id,
                    domain=DOMAIN_POS,
                    channel=mesa_channel,
                    action="payment_voided",
                    actor_user_id=user_id,
                    table_id=table_id,
                    table_session_id=session_row["id"],
                    order_id=target["order_id"],
                    reason=normalized_reason,
                    payload={
                        "voided_ids": voided_ids,
                        "order_ids": [str(oid) for oid in affected_order_ids],
                        "payment_method": target["payment_method"],
                        "amount": void_amount,
                        "cash_received": (
                            float(target["cash_received"])
                            if target["cash_received"] is not None
                            else None
                        ),
                        "reopened": reopened,
                    },
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

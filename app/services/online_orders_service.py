"""
Online Orders Service
Authenticated, tenant-scoped listing of online orders for restaurant operators.
"""
import asyncio
from decimal import Decimal
from typing import Optional
from uuid import UUID
from zoneinfo import ZoneInfo
from fastapi import Request
_BOG = ZoneInfo("America/Bogota")
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError, APIError, NotFoundError, ValidationError
from app.services.email_helpers import send_order_accepted_email
from app.services.waros_service import evaluate_and_award
from app.services.cierre_service import _get_tenant_tax_config, _post_order_gl_entry, _post_order_cogs_gl_entry
from app.services.ingredient_purchase_units_service import resolve_recipe_quantity_to_base_unit
from app.services.comandas_service import fire_comandas
# Issue #521 — reuse the POS snapshot writer so online orders also write
# order_item_ingredients (per-line ingredient cost). Without this the COGS
# GL entry posted right after deduction sums an empty table and skips,
# leaving online sales out of CMV. Same function, same idempotency guarantees.
from app.services.pos_cart_service import (
    _capture_order_item_ingredients,
    _deduct_modifier_inventory_for_order_item,
)
import logging

logger = logging.getLogger(__name__)


async def _deduct_stock_for_order(conn, order_id: UUID, tenant_id, changed_by) -> None:
    """
    Deduct ingredient stock for all items in an online order, mirroring POS logic.
    Runs inside an existing connection/transaction.
    """
    items = await conn.fetch(
        """
        SELECT oi.id AS order_item_id, oi.product_id, oi.quantity,
               p.name AS product_name, o.order_number
        FROM order_items oi
        JOIN product p ON p.id = oi.product_id
        JOIN orders o ON o.id = oi.order_id
        WHERE oi.order_id = $1
        """,
        order_id,
    )

    ingredients_query = """
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

    for item in items:
        # Issue #521: write the per-line ingredient snapshot first so the
        # subsequent _post_order_cogs_gl_entry (called by the caller after
        # this function returns) finds rows to sum. Idempotent via
        # UNIQUE (order_item_id, ingredient_id) — re-running is safe.
        await _capture_order_item_ingredients(
            conn,
            order_item_id=item["order_item_id"],
            product_id=item["product_id"],
            item_quantity=float(item["quantity"]),
            tenant_id=str(tenant_id),
        )

        ingredients = await conn.fetch(ingredients_query, item["product_id"])
        order_number = item["order_number"]

        for ingredient in ingredients:
            resolved_qty = await resolve_recipe_quantity_to_base_unit(
                conn,
                ingredient["ingredient_id"],
                float(ingredient["quantity"]),
                ingredient["unit"] or "",
            )
            quantity_to_deduct = float(item["quantity"]) * resolved_qty

            stock_row = await conn.fetchrow(
                "SELECT current_stock FROM tenant_inventory WHERE ingredient_id = $1 AND tenant_id = $2",
                ingredient["ingredient_id"], tenant_id,
            )

            previous_stock = float(stock_row["current_stock"]) if stock_row else 0.0
            new_stock = previous_stock - quantity_to_deduct

            if stock_row:
                await conn.execute(
                    "UPDATE tenant_inventory SET current_stock = $1, last_updated = NOW() WHERE ingredient_id = $2 AND tenant_id = $3",
                    new_stock, ingredient["ingredient_id"], tenant_id,
                )
            else:
                await conn.execute(
                    "INSERT INTO tenant_inventory (tenant_id, ingredient_id, current_stock, minimum_stock, last_updated) VALUES ($1, $2, $3, 0, NOW())",
                    tenant_id, ingredient["ingredient_id"], -quantity_to_deduct,
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
                f"Domicilio {item['quantity']}x {item['product_name']} - Orden #{order_number}",
                changed_by,
            )

            logger.info(
                f"Stock deducted (online order): {ingredient['ingredient_name']} "
                f"-{quantity_to_deduct}{ingredient['unit']} (Order #{order_number})"
            )

        modifier_rows = await conn.fetch(
            """
            SELECT modifier_id, modifier_name, quantity
            FROM order_item_modifiers
            WHERE order_item_id = $1
            """,
            item["order_item_id"],
        )
        for mod in modifier_rows:
            if not mod["modifier_id"]:
                continue
            modifier_qty = float(mod["quantity"]) if mod["quantity"] else 1.0
            await _deduct_modifier_inventory_for_order_item(
                conn,
                tenant_id=tenant_id,
                user_id=changed_by,
                order_id=order_id,
                order_item_id=item["order_item_id"],
                order_number=order_number,
                item_quantity=float(item["quantity"]),
                modifier={
                    "id": str(mod["modifier_id"]),
                    "name": mod["modifier_name"],
                },
                modifier_qty=modifier_qty,
            )


SORT_COLUMNS = {
    "order_number": "o.order_number",
    "order_date": "o.order_date",
    "scheduled_time": "o.scheduled_time",
    "total_amount": "o.total_amount",
    "status": "o.status",
}

ALLOWED_TRANSITIONS: dict[str, list[str]] = {
    "pending":   ["confirmed", "cancelled"],
    "confirmed": ["preparing", "cancelled"],
    "preparing": ["delivered", "cancelled"],
    "delivered": ["completed"],
    "completed": [],
    "cancelled": [],
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


async def update_order_status(
    request: Request,
    order_id: UUID,
    new_status: str,
    reason: Optional[str] = None,
    auto_complete: bool = False,
    payment_method: Optional[str] = None,
    payment_method_id: Optional[UUID] = None,
) -> dict:
    """
    Validate the status transition, then atomically:
      1. UPDATE orders SET status, updated_at (and optionally payment_method / payment_method_id)
      2. INSERT into order_status_history
    Returns the transition payload including change_date from history.

    When transitioning to 'delivered' the caller MUST provide payment_method
    if the order does not already have one persisted — otherwise the
    accounting GL hook downstream falls back to 'digital' and we lose the
    real payment information (see warocol.com#606).
    """
    try:
        session = require_valid_session(request)
        tenant_id = session.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        # user_id may be None for API key auth — column is nullable
        changed_by = session.user_id

        async with get_db_connection() as conn:
            # 1. Fetch current order (tenant-scoped, online orders only)
            row = await conn.fetchrow(
                """
                SELECT id, status, customer_id, payment_method, payment_method_id
                FROM orders
                WHERE id = $1
                  AND tenant_id = $2
                  AND online_cart_id IS NOT NULL
                """,
                order_id, tenant_id,
            )
            if not row:
                raise NotFoundError(f"Order {order_id} not found")

            old_status = row["status"]
            order_customer_id = row["customer_id"]  # may be None for guest checkout
            existing_payment_method = row["payment_method"]

            # 2. Validate state machine transition
            allowed = ALLOWED_TRANSITIONS.get(old_status, [])
            if new_status not in allowed:
                raise ValidationError(
                    f"Invalid transition from '{old_status}' to '{new_status}'. "
                    f"Allowed: {allowed if allowed else 'none (terminal state)'}"
                )

            # 2b. Validate payment-method capture on 'delivered' (warocol.com#606)
            if new_status == "delivered" and not existing_payment_method and not payment_method:
                from fastapi import HTTPException
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "payment_method_required",
                        "message": "Selecciona un método de pago para marcar como entregado.",
                    },
                )

            # 2c. Validate slug + UUID belong to this tenant before writing
            if payment_method:
                group_row = await conn.fetchrow(
                    """
                    SELECT id FROM payment_method_groups
                    WHERE slug = $1
                      AND is_active = true
                      AND (tenant_id IS NULL OR tenant_id = $2)
                    """,
                    payment_method, tenant_id,
                )
                if not group_row:
                    from fastapi import HTTPException
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "code": "payment_method_invalid",
                            "message": f"Método de pago '{payment_method}' no es válido para este restaurante.",
                        },
                    )
                if payment_method_id:
                    method_row = await conn.fetchrow(
                        """
                        SELECT id FROM payment_methods
                        WHERE id = $1
                          AND tenant_id = $2
                          AND group_id = $3
                          AND is_active = true
                        """,
                        payment_method_id, tenant_id, group_row["id"],
                    )
                    if not method_row:
                        from fastapi import HTTPException
                        raise HTTPException(
                            status_code=400,
                            detail={
                                "code": "payment_method_id_invalid",
                                "message": "El método seleccionado no pertenece al grupo elegido.",
                            },
                        )

            # 3. UPDATE orders.status + updated_at (+ optional payment fields via COALESCE).
            #    COALESCE preserves prior values when the caller omits them, so the
            #    follow-up delivered → completed transition doesn't need to re-send.
            await conn.execute(
                """
                UPDATE orders
                SET status = $1,
                    updated_at = NOW(),
                    payment_method = COALESCE($4, payment_method),
                    payment_method_id = COALESCE($5, payment_method_id)
                WHERE id = $2 AND tenant_id = $3
                """,
                new_status, order_id, tenant_id, payment_method, payment_method_id,
            )

            # 4. INSERT order_status_history, RETURNING change_date
            history_row = await conn.fetchrow(
                """
                INSERT INTO order_status_history
                    (order_id, old_status, new_status, changed_by, reason)
                VALUES ($1, $2, $3, $4::uuid, $5)
                RETURNING change_date
                """,
                order_id, old_status, new_status, changed_by, reason,
            )

            # 5. Auto-complete: if requested and this was a pending → confirmed transition,
            #    immediately execute a second confirmed → completed transition in the same conn.
            if auto_complete and old_status == "pending" and new_status == "confirmed":
                # 5a. UPDATE orders to completed
                await conn.execute(
                    """
                    UPDATE orders
                    SET status = $1, updated_at = NOW()
                    WHERE id = $2 AND tenant_id = $3
                    """,
                    "completed", order_id, tenant_id,
                )

                # Auto-fire hook for auto-completed orders (they skip 'preparing')
                try:
                    _prof = await conn.fetchrow(
                        "SELECT comandas_enabled FROM tenant_public_profiles WHERE tenant_id = $1",
                        tenant_id
                    )
                    if _prof and _prof["comandas_enabled"]:
                        _order_type_row = await conn.fetchrow(
                            "SELECT oc.order_type FROM online_carts oc JOIN orders o ON o.online_cart_id = oc.id WHERE o.id = $1",
                            order_id
                        )
                        _tbl_name = 'Domicilio' if _order_type_row and _order_type_row['order_type'] == 'delivery' else 'Pickup'
                        await fire_comandas(
                            order_id=order_id,
                            tenant_id=tenant_id,
                            source_type='delivery',
                            table_display_name=_tbl_name,
                            conn=conn
                        )
                except Exception as _fe:
                    logger.error(f"Auto-fire failed for auto-completed online order {order_id}: {_fe}")

                # 5b. INSERT second history row
                auto_history_row = await conn.fetchrow(
                    """
                    INSERT INTO order_status_history
                        (order_id, old_status, new_status, changed_by, reason)
                    VALUES ($1, $2, $3, $4::uuid, $5)
                    RETURNING change_date
                    """,
                    order_id, "confirmed", "completed", changed_by, None,
                )

                # Deduct stock for completed order
                try:
                    await _deduct_stock_for_order(conn, order_id, tenant_id, changed_by)
                except Exception as _stock_err:
                    logger.error(f"Stock deduction failed for order {order_id}: {_stock_err}")

                # GL journal entry — atomic with the order, failure never blocks
                try:
                    order_for_gl = await conn.fetchrow(
                        """SELECT order_number, total_amount, payment_method, payment_method_id,
                                  order_date, tip_amount, tip_tax_amount
                           FROM orders WHERE id = $1""",
                        order_id,
                    )
                    tax_config = await _get_tenant_tax_config(conn, tenant_id)
                    await _post_order_gl_entry(
                        conn=conn,
                        tenant_id=tenant_id,
                        order_id=order_id,
                        order_date=order_for_gl["order_date"].astimezone(_BOG).date(),
                        total_amount=Decimal(str(order_for_gl["total_amount"])),
                        payment_method=order_for_gl["payment_method"] or "digital",
                        payment_method_id=order_for_gl["payment_method_id"],
                        tax_config=tax_config,
                        order_number=int(order_for_gl["order_number"]),
                        tip_amount=Decimal(str(order_for_gl["tip_amount"] or 0)),
                        tip_tax_amount=Decimal(str(order_for_gl["tip_tax_amount"] or 0)),
                    )
                except Exception as e:
                    logger.error(f"GL entry failed for online order {order_id}: {e}")
                    # Do NOT re-raise — status update completes regardless

                # COGS GL entry — DR 6135 Costo de ventas / CR 1435 Inventarios
                try:
                    await _post_order_cogs_gl_entry(
                        conn=conn,
                        tenant_id=tenant_id,
                        order_id=order_id,
                        order_date=order_for_gl["order_date"].astimezone(_BOG).date(),
                        order_number=int(order_for_gl["order_number"]),
                    )
                except Exception as e:
                    logger.error(f"COGS GL entry failed for online order {order_id}: {e}")

                # Fire acceptance email (non-blocking — does not delay the response)
                email_row = await conn.fetchrow(
                    """
                    SELECT oc.verified_email, oc.order_type, o.order_number, o.total_amount,
                           ap.address_line1, ap.address_line2, ap.city, ap.delivery_notes
                    FROM orders o
                    JOIN online_carts oc ON oc.id = o.online_cart_id
                    LEFT JOIN addresses_profile ap ON ap.id = oc.delivery_address_id
                    WHERE o.id = $1
                    """,
                    order_id,
                )
                item_rows = await conn.fetch(
                    """
                    SELECT pr.name AS product_name, oi.quantity, oi.price_at_purchase, oi.subtotal
                    FROM order_items oi
                    JOIN product pr ON pr.id = oi.product_id
                    WHERE oi.order_id = $1
                    ORDER BY oi.created_at
                    """,
                    order_id,
                )

                if email_row and email_row["verified_email"]:
                    delivery_address = None
                    if email_row["address_line1"]:
                        delivery_address = {
                            "address_line1": email_row["address_line1"],
                            "address_line2": email_row["address_line2"],
                            "city": email_row["city"],
                            "delivery_notes": email_row["delivery_notes"],
                        }
                    asyncio.create_task(send_order_accepted_email(
                        customer_email=email_row["verified_email"],
                        order_number=int(email_row["order_number"]),
                        order_type=email_row["order_type"],
                        items=[dict(r) for r in item_rows],
                        subtotal=float(email_row["total_amount"]),
                        delivery_address=delivery_address,
                        order_id=str(order_id),
                        tenant_id=str(tenant_id),
                    ))

                # Award waros for auto-completed order (fire-and-forget — never blocks)
                if order_customer_id:
                    try:
                        asyncio.create_task(
                            evaluate_and_award(order_id, order_customer_id, tenant_id)
                        )
                    except Exception as _waros_err:
                        logger.warning(f"Could not schedule waros evaluation: {_waros_err}")

                # Override the return payload to reflect the final completed state
                return {
                    "success": True,
                    "data": {
                        "order_id": str(order_id),
                        "old_status": old_status,           # "pending"
                        "new_status": "completed",           # final state
                        "changed_at": auto_history_row["change_date"].isoformat(),
                        "reason": reason,
                        "auto_completed": True,
                        "transitions": [
                            {
                                "from": old_status,
                                "to": "confirmed",
                                "changed_at": history_row["change_date"].isoformat(),
                            },
                            {
                                "from": "confirmed",
                                "to": "completed",
                                "changed_at": auto_history_row["change_date"].isoformat(),
                            },
                        ],
                    },
                }

            # Deduct stock for direct completed transition
            if new_status == "completed":
                try:
                    await _deduct_stock_for_order(conn, order_id, tenant_id, changed_by)
                except Exception as _stock_err:
                    logger.error(f"Stock deduction failed for order {order_id}: {_stock_err}")

                # GL journal entry — atomic with the order, failure never blocks
                try:
                    order_for_gl = await conn.fetchrow(
                        """SELECT order_number, total_amount, payment_method, payment_method_id,
                                  order_date, tip_amount, tip_tax_amount
                           FROM orders WHERE id = $1""",
                        order_id,
                    )
                    tax_config = await _get_tenant_tax_config(conn, tenant_id)
                    await _post_order_gl_entry(
                        conn=conn,
                        tenant_id=tenant_id,
                        order_id=order_id,
                        order_date=order_for_gl["order_date"].astimezone(_BOG).date(),
                        total_amount=Decimal(str(order_for_gl["total_amount"])),
                        payment_method=order_for_gl["payment_method"] or "digital",
                        payment_method_id=order_for_gl["payment_method_id"],
                        tax_config=tax_config,
                        order_number=int(order_for_gl["order_number"]),
                        tip_amount=Decimal(str(order_for_gl["tip_amount"] or 0)),
                        tip_tax_amount=Decimal(str(order_for_gl["tip_tax_amount"] or 0)),
                    )
                except Exception as e:
                    logger.error(f"GL entry failed for online order {order_id}: {e}")
                    # Do NOT re-raise — status update completes regardless

                # COGS GL entry — DR 6135 Costo de ventas / CR 1435 Inventarios
                try:
                    await _post_order_cogs_gl_entry(
                        conn=conn,
                        tenant_id=tenant_id,
                        order_id=order_id,
                        order_date=order_for_gl["order_date"].astimezone(_BOG).date(),
                        order_number=int(order_for_gl["order_number"]),
                    )
                except Exception as e:
                    logger.error(f"COGS GL entry failed for online order {order_id}: {e}")

            # Award waros for direct completed transition (fire-and-forget — never blocks)
            if new_status == "completed" and order_customer_id:
                try:
                    asyncio.create_task(
                        evaluate_and_award(order_id, order_customer_id, tenant_id)
                    )
                except Exception as _waros_err:
                    logger.warning(f"Could not schedule waros evaluation: {_waros_err}")

            # Auto-fire hook for regular transitions to 'preparing'
            if new_status == "preparing":
                try:
                    _prof = await conn.fetchrow(
                        "SELECT comandas_enabled FROM tenant_public_profiles WHERE tenant_id = $1",
                        tenant_id
                    )
                    if _prof and _prof["comandas_enabled"]:
                        _order_type_row = await conn.fetchrow(
                            "SELECT oc.order_type FROM online_carts oc JOIN orders o ON o.online_cart_id = oc.id WHERE o.id = $1",
                            order_id
                        )
                        _tbl_name = 'Domicilio' if _order_type_row and _order_type_row['order_type'] == 'delivery' else 'Pickup'
                        await fire_comandas(
                            order_id=order_id,
                            tenant_id=tenant_id,
                            source_type='delivery',
                            table_display_name=_tbl_name,
                            conn=conn
                        )
                except Exception as _fe:
                    logger.error(f"Auto-fire failed for online order {order_id} (preparing): {_fe}")

            return {
                "success": True,
                "data": {
                    "order_id": str(order_id),
                    "old_status": old_status,
                    "new_status": new_status,
                    "changed_at": history_row["change_date"].isoformat(),
                    "reason": reason,
                },
            }

    except (AuthenticationError, NotFoundError, ValidationError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error updating status for order {order_id}: {str(e)}")
        raise APIError(f"Error updating order status: {str(e)}", status_code=500)


async def get_order_status_history(
    request: Request,
    order_id: UUID,
) -> dict:
    """
    Return all status transitions for an online order, ordered by change_date ASC.
    Tenant-scoped via JOIN to orders table.
    Returns empty list when order exists but has no transitions yet.
    """
    try:
        session = require_valid_session(request)
        tenant_id = session.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # 1. Verify order exists and belongs to this tenant (online orders only)
            row = await conn.fetchrow(
                """
                SELECT id FROM orders
                WHERE id = $1
                  AND tenant_id = $2
                  AND online_cart_id IS NOT NULL
                """,
                order_id, tenant_id,
            )
            if not row:
                raise NotFoundError(f"Order {order_id} not found")

            # 2. Fetch full status history
            history_rows = await conn.fetch(
                """
                SELECT
                    osh.id,
                    osh.old_status,
                    osh.new_status,
                    osh.change_date,
                    osh.reason
                FROM order_status_history osh
                JOIN orders o ON o.id = osh.order_id
                WHERE osh.order_id = $1
                  AND o.tenant_id = $2
                  AND o.online_cart_id IS NOT NULL
                ORDER BY osh.change_date ASC
                """,
                order_id, tenant_id,
            )

            return {
                "success": True,
                "data": [
                    {
                        "id": str(r["id"]),
                        "old_status": r["old_status"],
                        "new_status": r["new_status"],
                        "change_date": r["change_date"].isoformat(),
                        "reason": r["reason"],
                    }
                    for r in history_rows
                ],
            }

    except (AuthenticationError, NotFoundError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting status history for order {order_id}: {str(e)}")
        raise APIError(f"Error getting order status history: {str(e)}", status_code=500)

"""
POS Cart Service
Handles cart persistence for POS system
"""
import asyncio
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence
from uuid import UUID
from datetime import date, datetime
from fastapi import Request
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError, APIError
from app.core.timezones import get_zoneinfo, local_date_for_tenant, resolve_tenant_timezone
from app.services.waros_service import evaluate_and_award
from app.services.orders_service import _get_order_waro_redemption_summary
from app.services.email_helpers import send_pos_receipt_email
from app.services import invoice_email_tracking_service
from app.services.cierre_service import (
    _get_tenant_tax_config,
    _post_order_gl_entry,
    _post_order_cogs_gl_entry,
    _post_deferred_order_tip_gl,
)
from app.services.tip_tax_service import (
    compute_tip_tax_amount,
    normalize_tip_payload,
    split_settlement_amount_due,
    tip_settlement_total,
)
from app.services.accounting_service import void_order_journal_entry_in_txn
from app.services.ingredient_purchase_units_service import resolve_recipe_quantity_to_base_unit
from app.services.comandas_service import fire_comandas, finalize_open_comandas
from app.services.operation_events_service import DOMAIN_POS, record_operation_event
from app.services.customer_wallet_service import (
    WALLET_PAYMENT_SLUG,
    apply_wallet_for_order,
    assert_wallet_customer_identified,
    restore_wallet_for_order_payment_void,
    validate_wallet_payment_tender,
)
from app.services.open_priced_service import (
    fetch_product_pricing_map,
    resolve_line_unit_price,
    validate_items_unit_prices,
)
from app.services.modifier_option_service import (
    modifier_line_subtotal,
    resolve_modifier_selections,
)
import logging

logger = logging.getLogger(__name__)


def _modifier_snapshot_total(modifier: Dict[str, Any]) -> float:
    return float(
        modifier_line_subtotal(
            modifier.get("price", 0),
            modifier.get("quantity") or 1,
            modifier.get("included_quantity") or 0,
        )
    )


async def _order_payment_splits_for_gl(conn, order_id: UUID) -> List[Dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT amount, payment_method, payment_method_id
        FROM order_payments
        WHERE order_id = $1 AND voided_at IS NULL
        ORDER BY paid_at ASC, id ASC
        """,
        order_id,
    )
    return [
        {
            "amount": Decimal(str(row["amount"])),
            "payment_method": row["payment_method"],
            "payment_method_id": row["payment_method_id"],
        }
        for row in rows
    ]


def _distribute_discount(items: List[dict], discount_amount: float) -> List[dict]:
    """
    Distribute a discount proportionally across cart items based on each item's
    share of the total subtotal. Rounding remainder assigned to the largest item.

    Args:
        items: list of dicts with at least a 'subtotal' key (float)
        discount_amount: total discount to distribute (already computed, in COP integer)

    Returns:
        Same list with 'discount_allocated' and 'net_total' added to each item.
    """
    total_subtotal = sum(item['subtotal'] for item in items)
    if total_subtotal <= 0 or discount_amount <= 0:
        for item in items:
            item['discount_allocated'] = 0.0
            item['net_total'] = float(item['subtotal'])
        return items

    allocated_total = 0.0
    for item in items:
        share = float(item['subtotal']) / total_subtotal
        item['discount_allocated'] = round(discount_amount * share)
        item['net_total'] = float(item['subtotal']) - item['discount_allocated']
        allocated_total += item['discount_allocated']

    # Assign rounding remainder to the item with the largest subtotal
    remainder = round(discount_amount) - round(allocated_total)
    if remainder != 0:
        largest = max(items, key=lambda x: x['subtotal'])
        largest['discount_allocated'] += remainder
        largest['net_total'] -= remainder

    return items


def _cart_items_to_promo_lines(items: List[dict]) -> List[dict]:
    lines = []
    for item in items:
        quantity = item.get("quantity") or 1
        raw_unit_price = item.get("unit_price")
        if raw_unit_price is None:
            raw_unit_price = item.get("product", {}).get("price")
        eligible_modifier_total = sum(
            _modifier_snapshot_total(mod)
            for mod in item.get("modifiers", [])
            if mod.get("group_is_required") or mod.get("is_default")
        )
        line = {
            "id": item["id"],
            "product_id": item.get("product_id") or item["product"]["id"],
            "category_id": item.get("category_id"),
            "quantity": quantity,
            "subtotal": float(item["subtotal"]),
            "tax_category": item.get("tax_category") or "standard",
            "promo_opt_out": bool(item.get("promo_opt_out")),
        }
        if raw_unit_price is not None:
            line["promo_eligible_subtotal"] = (
                float(raw_unit_price) + eligible_modifier_total
            ) * quantity
        for key in (
            "locked_promotion_id",
            "locked_promotion_name",
            "locked_promo_type",
            "locked_promo_savings",
        ):
            if item.get(key) is not None:
                line[key] = item[key]
        lines.append(line)
    return lines


async def _refresh_cart_item_promotion_lock(
    conn,
    tenant_id: UUID,
    cart_id: UUID,
    item_id: UUID,
) -> None:
    reset_ids = {str(item_id)}
    await conn.execute(
        """
        UPDATE pos_cart_items
        SET locked_promotion_id = NULL,
            promotion_locked_at = NULL,
            locked_promo_eligible_subtotal = NULL,
            locked_promo_eligible_unit_price = NULL,
            locked_promotion_name = NULL,
            locked_promo_type = NULL,
            locked_promo_savings = NULL
        WHERE id = $1
        """,
        item_id,
    )
    items = await get_cart_items(conn, cart_id)
    if not items:
        return

    from app.services.promotions_service import evaluate_checkout_promotions

    # Operational tenant TZ (explicit → profile → America/Bogota); not fiscal CO time.
    tenant_timezone = await resolve_tenant_timezone(conn, tenant_id)
    locked_at = datetime.now(get_zoneinfo(tenant_timezone))
    promo_lines = _cart_items_to_promo_lines(items)
    checkout_eval = await evaluate_checkout_promotions(
        conn,
        UUID(str(tenant_id)),
        promo_lines,
        at=locked_at,
    )
    items_by_id = {item["id"]: item for item in items}
    promo_lines_by_id = {line["id"]: line for line in promo_lines}
    for eval_line in checkout_eval.get("lines") or []:
        line_id = eval_line["id"]
        item = items_by_id.get(line_id)
        if not item or item.get("promo_opt_out"):
            continue
        if line_id not in reset_ids and item.get("locked_promotion_id"):
            continue
        promo_id = eval_line.get("promotion_id")
        promo_savings = round(float(eval_line.get("promo_savings") or 0))
        if not promo_id or promo_savings <= 0:
            continue

        promo_line = promo_lines_by_id[line_id]
        eligible_subtotal = float(
            promo_line.get("promo_eligible_subtotal") or promo_line.get("subtotal") or 0
        )
        quantity = int(promo_line.get("quantity") or 1)
        eligible_unit_price = eligible_subtotal / quantity if quantity > 0 else 0
        await conn.execute(
            """
            UPDATE pos_cart_items
            SET locked_promotion_id = $1,
                promotion_locked_at = $2,
                locked_promo_eligible_subtotal = $3,
                locked_promo_eligible_unit_price = $4,
                locked_promotion_name = $5,
                locked_promo_type = $6,
                locked_promo_savings = $7
            WHERE id = $8
            """,
            UUID(str(promo_id)),
            locked_at,
            eligible_subtotal,
            eligible_unit_price,
            eval_line.get("promotion_name"),
            eval_line.get("promo_type"),
            promo_savings,
            UUID(str(line_id)),
        )


def _manual_discount_amount(
    subtotal: float,
    discount_type: Optional[str],
    discount_value: Optional[float],
) -> float:
    if not discount_type or discount_value is None or discount_value <= 0:
        return 0.0
    if discount_type == "percent":
        return float(round(subtotal * discount_value / 100))
    return float(min(round(discount_value), round(subtotal)))


def _tax_rows_from_evaluated_lines(lines: Sequence[dict]) -> List[dict]:
    grouped: Dict[str, float] = {}
    for line in lines:
        category = line.get("tax_category") or "standard"
        grouped[category] = grouped.get(category, 0.0) + float(
            line.get("net_total", line.get("subtotal_after_promo", line["subtotal"]))
        )
    return [{"tax_category": k, "subtotal": v} for k, v in grouped.items()]


async def get_or_create_active_cart(
    request: Request,
    customer_id: UUID,
    session_id: Optional[str] = None
) -> dict:
    """
    Get active cart for customer or create new one
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Try to find active cart for this customer
            cart_query = """
                SELECT id, total_amount, created_at, updated_at
                FROM pos_carts
                WHERE tenant_id = $1
                AND customer_id = $2
                AND status = 'active'
                ORDER BY updated_at DESC
                LIMIT 1
            """
            cart_row = await conn.fetchrow(cart_query, tenant_id, customer_id)

            if cart_row:
                # Return existing cart
                cart_id = cart_row['id']
                logger.info(f"Found existing cart: {cart_id}")
            else:
                # Create new cart
                create_cart_query = """
                    INSERT INTO pos_carts (
                        tenant_id, user_id, customer_id, session_id, status, total_amount
                    )
                    VALUES ($1, $2, $3, $4, 'active', 0)
                    RETURNING id, total_amount, created_at, updated_at
                """
                cart_row = await conn.fetchrow(
                    create_cart_query,
                    tenant_id,
                    user_id,
                    customer_id,
                    session_id
                )
                cart_id = cart_row['id']
                logger.info(f"Created new cart: {cart_id}")

            # Get cart items with modifiers
            items = await get_cart_items(conn, cart_id)

            # Issue warocol.com#656 — active (non-voided) partial payments of the
            # cart's order, surfaced so checkout can rehydrate "Pagos registrados"
            # on re-entry (otherwise the cashier may double-charge).
            # POS payments are not split proportionally — one row per logical
            # payment. Bar mode filter via order state (only orders still in flight).
            partial_payments_rows = await conn.fetch(
                """
                SELECT op.id, op.amount, op.payment_method, op.payment_method_id,
                       op.paid_at, pm.name AS payment_method_name
                FROM order_payments op
                LEFT JOIN payment_methods pm ON pm.id = op.payment_method_id
                WHERE op.order_id = (
                    SELECT id FROM orders
                    WHERE pos_cart_id = $1
                      AND (status != 'completed' OR payment_status != 'paid')
                    ORDER BY created_at DESC
                    LIMIT 1
                )
                  AND op.voided_at IS NULL
                ORDER BY op.paid_at
                """,
                cart_id,
            )

            return {
                "success": True,
                "data": {
                    "id": str(cart_id),
                    "total_amount": float(cart_row['total_amount']),
                    "items": items,
                    "created_at": cart_row['created_at'].isoformat(),
                    "updated_at": cart_row['updated_at'].isoformat(),
                    # Issue warocol.com#656 — rehydration source for checkout's Pagos registrados
                    "partial_payments": [
                        {
                            "id": str(r["id"]),
                            "amount": float(r["amount"]),
                            "payment_method": r["payment_method"],
                            "payment_method_id": str(r["payment_method_id"]) if r["payment_method_id"] else None,
                            "payment_method_name": r["payment_method_name"],
                            "paid_at": r["paid_at"].isoformat(),
                        }
                        for r in partial_payments_rows
                    ],
                }
            }

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting/creating cart: {str(e)}")
        raise APIError(f"Error getting/creating cart: {str(e)}", status_code=500)


async def create_cart_with_batch_items(
    request: Request,
    customer_id: Optional[UUID],
    items: List[dict]
) -> dict:
    """
    Create a cart and add all items in a single transaction (batch).
    customer_id is optional - if None, creates anonymous cart.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        if not items:
            raise APIError("Cannot create cart without items", status_code=400)

        async with get_db_connection() as conn:
            async with conn.transaction():
                # Create cart (customer_id can be None)
                create_cart_query = """
                    INSERT INTO pos_carts (
                        tenant_id, user_id, customer_id, status, total_amount
                    )
                    VALUES ($1, $2, $3, 'active', 0)
                    RETURNING id, total_amount, created_at, updated_at
                """
                cart_row = await conn.fetchrow(
                    create_cart_query,
                    tenant_id,
                    user_id,
                    customer_id  # Can be None
                )
                cart_id = cart_row['id']
                logger.info(f"Created cart {cart_id} (customer: {customer_id or 'anonymous'})")

                product_ids = list({item["product_id"] for item in items})
                pricing_map = await fetch_product_pricing_map(conn, tenant_id, product_ids)
                validate_items_unit_prices(pricing_map, items)

                # Add all items in batch
                item_ids = []
                for item in items:
                    product_id = item['product_id']
                    quantity = item['quantity']
                    unit_price = item['unit_price']
                    modifiers = item.get('modifiers', [])
                    notes = item.get('notes')
                    modifiers = await resolve_modifier_selections(
                        conn, UUID(str(product_id)), modifiers
                    )

                    # Calculate subtotal
                    modifiers_total = sum(_modifier_snapshot_total(mod) for mod in modifiers)
                    subtotal = (unit_price + modifiers_total) * quantity

                    # Insert cart item
                    item_query = """
                        INSERT INTO pos_cart_items (
                            cart_id, product_id, quantity, unit_price, subtotal, notes
                        )
                        VALUES ($1, $2, $3, $4, $5, $6)
                        RETURNING id
                    """
                    item_row = await conn.fetchrow(
                        item_query,
                        cart_id,
                        product_id,
                        quantity,
                        unit_price,
                        subtotal,
                        notes
                    )
                    item_id = item_row['id']
                    item_ids.append(str(item_id))

                    # Insert modifiers
                    if modifiers:
                        modifier_query = """
                            INSERT INTO pos_cart_item_modifiers (
                                cart_item_id, modifier_id, modifier_name, price,
                                quantity, included_quantity
                            )
                            VALUES ($1, $2, $3, $4, $5, $6)
                        """
                        for mod in modifiers:
                            await conn.execute(
                                modifier_query,
                                item_id,
                                mod['id'],
                                mod['name'],
                                mod['price'],
                                mod["quantity"],
                                mod["included_quantity"],
                            )

                    await _refresh_cart_item_promotion_lock(conn, tenant_id, cart_id, item_id)

                # Update cart total
                await update_cart_total(conn, cart_id)

                # Get updated total
                total_row = await conn.fetchrow(
                    "SELECT total_amount FROM pos_carts WHERE id = $1",
                    cart_id
                )

                logger.info(f"Added {len(items)} items to cart {cart_id} in batch")

                return {
                    "success": True,
                    "message": f"Cart created with {len(items)} items",
                    "data": {
                        "cart_id": str(cart_id),
                        "item_ids": item_ids,
                        "total_amount": float(total_row['total_amount']),
                        "items_count": len(items)
                    }
                }

    except AuthenticationError as e:
        raise e
    except APIError as e:
        raise e
    except Exception as e:
        logger.error(f"Error creating cart with batch items: {str(e)}")
        raise APIError(f"Error creating cart with batch items: {str(e)}", status_code=500)


async def get_cart_items(conn, cart_id: UUID) -> List[dict]:
    """
    Get all items in a cart with their modifiers.
    Single query using json_agg to avoid N+1 round-trips.
    """
    items_query = """
        SELECT
            ci.id,
            ci.product_id,
            ci.quantity,
            ci.unit_price,
            ci.subtotal,
            ci.notes,
            ci.promo_opt_out,
            ci.locked_promotion_id,
            ci.promotion_locked_at,
            ci.locked_promo_eligible_subtotal,
            ci.locked_promo_eligible_unit_price,
            ci.locked_promotion_name,
            ci.locked_promo_type,
            ci.locked_promo_savings,
            p.name as product_name,
            p.category_id as category_id,
            COALESCE(p.tax_category, 'standard') AS tax_category,
            p.is_resale as product_is_resale,
            COALESCE(
                json_agg(
                    json_build_object(
                        'id', m.modifier_id::text,
                        'name', m.modifier_name,
                        'price', m.price,
                        'quantity', m.quantity,
                        'included_quantity', m.included_quantity,
                        'is_default', COALESCE(mod.is_default, false),
                        'group_is_required', COALESCE(mg.is_required, false)
                    ) ORDER BY m.created_at
                ) FILTER (WHERE m.cart_item_id IS NOT NULL),
                '[]'::json
            ) as modifiers
        FROM pos_cart_items ci
        JOIN product p ON ci.product_id = p.id
        LEFT JOIN pos_cart_item_modifiers m ON m.cart_item_id = ci.id
        LEFT JOIN modifiers mod ON mod.id = m.modifier_id
        LEFT JOIN modifier_groups mg ON mg.id = mod.modifier_group_id
        WHERE ci.cart_id = $1
        GROUP BY ci.id, ci.product_id, ci.quantity, ci.unit_price, ci.subtotal,
                 ci.notes, ci.promo_opt_out, ci.locked_promotion_id, ci.promotion_locked_at,
                 ci.locked_promo_eligible_subtotal, ci.locked_promo_eligible_unit_price,
                 ci.locked_promotion_name, ci.locked_promo_type, ci.locked_promo_savings,
                 ci.created_at, p.name, p.category_id, p.tax_category, p.is_resale
        ORDER BY ci.created_at
    """
    items_rows = await conn.fetch(items_query, cart_id)

    items = []
    for item_row in items_rows:
        raw_modifiers = item_row['modifiers']
        if isinstance(raw_modifiers, str):
            import json
            raw_modifiers = json.loads(raw_modifiers)

        items.append({
            "id": str(item_row['id']),
            "product_id": str(item_row['product_id']),
            "category_id": str(item_row['category_id']) if item_row['category_id'] else None,
            "tax_category": item_row['tax_category'] or 'standard',
            "product": {
                "id": str(item_row['product_id']),
                "name": item_row['product_name'],
                "image": None,  # No hay columna image_url en la tabla product
                "price": float(item_row['unit_price'])
            },
            "quantity": item_row['quantity'],
            "is_resale": item_row['product_is_resale'] or False,
            "modifiers": [
                {
                    "id": mod['id'],
                    "name": mod['name'],
                    "price": float(mod['price']),
                    "quantity": int(mod.get("quantity") or 1),
                    "included_quantity": int(mod.get("included_quantity") or 0),
                    "is_default": bool(mod.get("is_default")),
                    "group_is_required": bool(mod.get("group_is_required")),
                }
                for mod in raw_modifiers
            ],
            "notes": item_row['notes'],
            "subtotal": float(item_row['subtotal']),
            "promo_opt_out": bool(item_row.get("promo_opt_out")),
            "locked_promotion_id": (
                str(item_row["locked_promotion_id"])
                if item_row["locked_promotion_id"] else None
            ),
            "promotion_locked_at": item_row["promotion_locked_at"],
            "locked_promo_eligible_subtotal": (
                float(item_row["locked_promo_eligible_subtotal"])
                if item_row["locked_promo_eligible_subtotal"] is not None else None
            ),
            "locked_promo_eligible_unit_price": (
                float(item_row["locked_promo_eligible_unit_price"])
                if item_row["locked_promo_eligible_unit_price"] is not None else None
            ),
            "locked_promotion_name": item_row["locked_promotion_name"],
            "locked_promo_type": item_row["locked_promo_type"],
            "locked_promo_savings": (
                float(item_row["locked_promo_savings"])
                if item_row["locked_promo_savings"] is not None else None
            ),
        })

    return items


async def _require_promo_line_opt_out_enabled(conn, tenant_id: UUID) -> None:
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


async def update_cart_item_promo_opt_out(
    request: Request,
    cart_id: UUID,
    item_id: UUID,
    promo_opt_out: bool,
) -> dict:
    """Toggle per-line promotion opt-out for a POS cart item (warocol.com#1003)."""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT pci.id
                    FROM pos_cart_items pci
                    JOIN pos_carts pc ON pc.id = pci.cart_id
                    WHERE pci.id = $1 AND pci.cart_id = $2 AND pc.tenant_id = $3
                      AND pc.status = 'active'
                    """,
                    item_id,
                    cart_id,
                    tenant_id,
                )
                if not row:
                    raise APIError("Cart item not found", status_code=404)

                await _require_promo_line_opt_out_enabled(conn, tenant_id)

                await conn.execute(
                    """
                    UPDATE pos_cart_items
                    SET promo_opt_out = $1
                    WHERE id = $2
                    """,
                    promo_opt_out,
                    item_id,
                )
                await _refresh_cart_item_promotion_lock(conn, tenant_id, cart_id, item_id)

        return {
            "success": True,
            "data": {"item_id": str(item_id), "promo_opt_out": promo_opt_out},
        }
    except (AuthenticationError, APIError):
        raise
    except Exception as e:
        logger.error(f"Error updating promo opt-out for cart item {item_id}: {e}")
        raise APIError(f"Error updating promo opt-out: {e}", status_code=500)


async def add_item_to_cart(
    request: Request,
    cart_id: UUID,
    product_id: UUID,
    quantity: int,
    unit_price: float,
    modifiers: List[dict],
    notes: Optional[str] = None
) -> dict:
    """
    Add an item to cart with modifiers
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                pricing_map = await fetch_product_pricing_map(conn, tenant_id, [product_id])
                unit_price = float(
                    resolve_line_unit_price(
                        pricing_map, product_id, unit_price, modifiers
                    )
                )
                modifiers = await resolve_modifier_selections(conn, product_id, modifiers)

                # Calculate subtotal
                modifiers_total = sum(_modifier_snapshot_total(mod) for mod in modifiers)
                subtotal = (unit_price + modifiers_total) * quantity

                # Insert cart item
                item_query = """
                    INSERT INTO pos_cart_items (
                        cart_id, product_id, quantity, unit_price, subtotal, notes
                    )
                    VALUES ($1, $2, $3, $4, $5, $6)
                    RETURNING id
                """
                item_row = await conn.fetchrow(
                    item_query,
                    cart_id,
                    product_id,
                    quantity,
                    unit_price,
                    subtotal,
                    notes
                )
                item_id = item_row['id']

                # Insert modifiers
                if modifiers:
                    modifier_query = """
                        INSERT INTO pos_cart_item_modifiers (
                            cart_item_id, modifier_id, modifier_name, price,
                            quantity, included_quantity
                        )
                        VALUES ($1, $2, $3, $4, $5, $6)
                    """
                    for mod in modifiers:
                        await conn.execute(
                            modifier_query,
                            item_id,
                            mod['id'],
                            mod['name'],
                            mod['price'],
                            mod["quantity"],
                            mod["included_quantity"],
                        )

                await _refresh_cart_item_promotion_lock(conn, tenant_id, cart_id, item_id)

                # Update cart total
                await update_cart_total(conn, cart_id)

                logger.info(f"Added item {item_id} to cart {cart_id}")

                return {
                    "success": True,
                    "message": "Item added to cart",
                    "data": {"item_id": str(item_id)}
                }

    except AuthenticationError as e:
        raise e
    except APIError as e:
        raise e
    except Exception as e:
        logger.error(f"Error adding item to cart: {str(e)}")
        raise APIError(f"Error adding item to cart: {str(e)}", status_code=500)


async def update_cart_item(
    request: Request,
    cart_id: UUID,
    item_id: UUID,
    quantity: int,
    unit_price: float,
    modifiers: List[dict],
    notes: Optional[str] = None
) -> dict:
    """
    Update a cart item with new quantity, modifiers, and notes.
    Replaces all modifiers with the new list.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                # Verify item exists and belongs to this cart
                verify_query = """
                    SELECT id FROM pos_cart_items
                    WHERE id = $1 AND cart_id = $2
                """
                item_exists = await conn.fetchrow(verify_query, item_id, cart_id)

                if not item_exists:
                    raise APIError("Cart item not found", status_code=404)

                product_row = await conn.fetchrow(
                    """
                    SELECT pci.product_id
                    FROM pos_cart_items pci
                    JOIN pos_carts pc ON pc.id = pci.cart_id
                    WHERE pci.id = $1 AND pci.cart_id = $2 AND pc.tenant_id = $3
                    """,
                    item_id,
                    cart_id,
                    tenant_id,
                )
                if not product_row:
                    raise APIError("Cart item not found", status_code=404)

                pricing_map = await fetch_product_pricing_map(
                    conn, tenant_id, [product_row["product_id"]]
                )
                unit_price = float(
                    resolve_line_unit_price(
                        pricing_map,
                        product_row["product_id"],
                        unit_price,
                        modifiers,
                    )
                )
                modifiers = await resolve_modifier_selections(
                    conn, product_row["product_id"], modifiers
                )

                # Calculate new subtotal
                modifiers_total = sum(_modifier_snapshot_total(mod) for mod in modifiers)
                subtotal = (unit_price + modifiers_total) * quantity

                # Update cart item
                update_query = """
                    UPDATE pos_cart_items
                    SET quantity = $1, unit_price = $2, subtotal = $3, notes = $4
                    WHERE id = $5
                """
                await conn.execute(
                    update_query,
                    quantity,
                    unit_price,
                    subtotal,
                    notes,
                    item_id
                )

                # Delete existing modifiers
                await conn.execute(
                    "DELETE FROM pos_cart_item_modifiers WHERE cart_item_id = $1",
                    item_id
                )

                # Insert new modifiers
                if modifiers:
                    modifier_query = """
                        INSERT INTO pos_cart_item_modifiers (
                            cart_item_id, modifier_id, modifier_name, price,
                            quantity, included_quantity
                        )
                        VALUES ($1, $2, $3, $4, $5, $6)
                    """
                    for mod in modifiers:
                        await conn.execute(
                            modifier_query,
                            item_id,
                            mod['id'],
                            mod['name'],
                            mod['price'],
                            mod["quantity"],
                            mod["included_quantity"],
                        )

                await _refresh_cart_item_promotion_lock(conn, tenant_id, cart_id, item_id)

                # Update cart total
                await update_cart_total(conn, cart_id)

                logger.info(f"Updated item {item_id} in cart {cart_id}")

                return {
                    "success": True,
                    "message": "Item updated successfully",
                    "data": {"item_id": str(item_id)}
                }

    except AuthenticationError as e:
        raise e
    except APIError as e:
        raise e
    except Exception as e:
        logger.error(f"Error updating cart item: {str(e)}")
        raise APIError(f"Error updating cart item: {str(e)}", status_code=500)


def _normalize_cart_channel(channel: Optional[str]) -> str:
    """POS cart channel for bitácora (#784)."""
    normalized = (channel or "mostrador").strip().lower()
    if normalized not in ("mostrador", "barra"):
        return "mostrador"
    return normalized


async def _fetch_cart_item_modifiers(conn, cart_item_id: UUID) -> List[Dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT modifier_id, modifier_name, price, quantity, included_quantity
        FROM pos_cart_item_modifiers
        WHERE cart_item_id = $1
        """,
        cart_item_id,
    )
    return [
        {
            "id": str(r["modifier_id"]) if r["modifier_id"] else None,
            "name": r["modifier_name"],
            "price": float(r["price"]),
            "quantity": int(r["quantity"] or 1),
            "included_quantity": int(r["included_quantity"] or 0),
        }
        for r in rows
    ]


def _build_cart_line_payload(
    *,
    product_id: Any,
    product_name: Optional[str],
    quantity: Any,
    unit_price: Any,
    subtotal: Any,
    modifiers: List[Dict[str, Any]],
    notes: Optional[str],
    table_id: Optional[UUID] = None,
    table_name: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "product_id": str(product_id) if product_id else None,
        "product_name": product_name,
        "quantity": float(quantity) if quantity is not None else None,
        "unit_price": float(unit_price),
        "subtotal": float(subtotal),
        "modifiers": modifiers,
        "notes": notes,
        "table_id": str(table_id) if table_id else None,
        "table_name": table_name,
        "order_number": None,
    }


async def _record_cart_operation_event(
    conn,
    tenant_id: UUID,
    *,
    user_id: UUID,
    channel: str,
    action: str,
    pos_cart_id: UUID,
    pos_cart_item_id: Optional[UUID] = None,
    payload: Optional[Dict[str, Any]] = None,
    actor_member_id: Optional[UUID] = None,
    reason: Optional[str] = None,
) -> None:
    merged_payload = dict(payload) if payload else {}
    if pos_cart_item_id:
        merged_payload["pos_cart_item_id"] = str(pos_cart_item_id)
    await record_operation_event(
        conn,
        tenant_id,
        domain=DOMAIN_POS,
        channel=_normalize_cart_channel(channel),
        action=action,
        actor_user_id=user_id,
        actor_member_id=actor_member_id,
        pos_cart_id=pos_cart_id,
        payload=merged_payload,
        reason=reason,
    )


def _normalize_cart_audit_reason(reason: Optional[str]) -> Optional[str]:
    normalized = (reason or "").strip()
    return normalized or None


async def remove_item_from_cart(
    request: Request,
    cart_id: UUID,
    item_id: UUID,
    channel: str = "mostrador",
    actor_member_id: Optional[UUID] = None,
    reason: Optional[str] = None,
) -> dict:
    """
    Remove an item from cart
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT
                        pci.id, pci.product_id, pci.quantity, pci.unit_price,
                        pci.subtotal, pci.notes,
                        p.name AS product_name
                    FROM pos_cart_items pci
                    JOIN pos_carts pc ON pc.id = pci.cart_id
                    JOIN product p ON p.id = pci.product_id
                    WHERE pci.id = $1 AND pci.cart_id = $2 AND pc.tenant_id = $3
                    """,
                    item_id,
                    cart_id,
                    tenant_id,
                )
                if not row:
                    raise APIError("Cart item not found", status_code=404)

                modifiers = await _fetch_cart_item_modifiers(conn, item_id)
                await _record_cart_operation_event(
                    conn,
                    tenant_id,
                    user_id=user_id,
                    channel=channel,
                    action="cart_line_removed",
                    pos_cart_id=cart_id,
                    pos_cart_item_id=item_id,
                    actor_member_id=actor_member_id,
                    reason=_normalize_cart_audit_reason(reason),
                    payload=_build_cart_line_payload(
                        product_id=row["product_id"],
                        product_name=row["product_name"],
                        quantity=row["quantity"],
                        unit_price=row["unit_price"],
                        subtotal=row["subtotal"],
                        modifiers=modifiers,
                        notes=row["notes"],
                    ),
                )

                await conn.execute(
                    """
                    DELETE FROM pos_cart_items
                    WHERE id = $1 AND cart_id = $2
                    """,
                    item_id,
                    cart_id,
                )

                await update_cart_total(conn, cart_id)

                logger.info(f"Removed item {item_id} from cart {cart_id}")

                return {
                    "success": True,
                    "message": "Item removed from cart"
                }

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error removing item from cart: {str(e)}")
        raise APIError(f"Error removing item from cart: {str(e)}", status_code=500)


async def clear_cart(
    request: Request,
    cart_id: UUID,
    channel: str = "mostrador",
    actor_member_id: Optional[UUID] = None,
    reason: Optional[str] = None,
) -> dict:
    """
    Clear all items from cart
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                cart_row = await conn.fetchrow(
                    """
                    SELECT id FROM pos_carts
                    WHERE id = $1 AND tenant_id = $2
                    """,
                    cart_id,
                    tenant_id,
                )
                if not cart_row:
                    raise APIError("Cart not found", status_code=404)

                lines = await conn.fetch(
                    """
                    SELECT
                        pci.id AS cart_item_id,
                        pci.product_id,
                        pci.quantity,
                        pci.unit_price,
                        pci.subtotal,
                        pci.notes,
                        p.name AS product_name
                    FROM pos_cart_items pci
                    JOIN product p ON p.id = pci.product_id
                    WHERE pci.cart_id = $1
                    """,
                    cart_id,
                )
                for line in lines:
                    line_modifiers = await _fetch_cart_item_modifiers(
                        conn, line["cart_item_id"]
                    )
                    await _record_cart_operation_event(
                        conn,
                        tenant_id,
                        user_id=user_id,
                        channel=channel,
                        action="cart_cleared",
                        pos_cart_id=cart_id,
                        pos_cart_item_id=line["cart_item_id"],
                        actor_member_id=actor_member_id,
                        reason=_normalize_cart_audit_reason(reason),
                        payload=_build_cart_line_payload(
                            product_id=line["product_id"],
                            product_name=line["product_name"],
                            quantity=line["quantity"],
                            unit_price=line["unit_price"],
                            subtotal=line["subtotal"],
                            modifiers=line_modifiers,
                            notes=line["notes"],
                        ),
                    )

                await conn.execute(
                    """
                    DELETE FROM pos_cart_items
                    WHERE cart_id = $1
                    """,
                    cart_id,
                )

                # Update cart total to 0
                update_query = """
                    UPDATE pos_carts
                    SET total_amount = 0
                    WHERE id = $1
                """
                await conn.execute(update_query, cart_id)

                logger.info(f"Cleared cart {cart_id}")

                return {
                    "success": True,
                    "message": "Cart cleared"
                }

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error clearing cart: {str(e)}")
        raise APIError(f"Error clearing cart: {str(e)}", status_code=500)


async def update_cart_total(conn, cart_id: UUID):
    """
    Recalculate and update cart total
    """
    total_query = """
        UPDATE pos_carts
        SET total_amount = (
            SELECT COALESCE(SUM(subtotal), 0)
            FROM pos_cart_items
            WHERE cart_id = $1
        )
        WHERE id = $1
    """
    await conn.execute(total_query, cart_id)


async def get_cart_tax_preview(
    request: Request,
    cart_id: UUID,
    discount_amount: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Issue #526 — preview the tax breakdown for an in-flight POS cart.

    Issue #982 — evaluates tenant promotions first, then applies optional
    manual discount_amount on the promo-adjusted subtotal.
    """
    from app.services.orders_service import _compute_tax_breakdown
    from app.services.promotions_service import evaluate_checkout_promotions

    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    async with get_db_connection() as conn:
        cart_row = await conn.fetchrow(
            "SELECT id FROM pos_carts WHERE id = $1 AND tenant_id = $2 AND status = 'active'",
            cart_id,
            tenant_id,
        )
        if not cart_row:
            raise APIError("Cart not found or already completed", status_code=404)

        items = await get_cart_items(conn, cart_id)
        if not items:
            return {
                "standard_tax": 0.0,
                "liquor_tax": 0.0,
                "standard_tax_label": "Impuesto",
                "subtotal": 0,
                "promo_savings": 0,
                "subtotal_after_promos": 0,
                "promo_breakdown": [],
                "lines": [],
            }

        promo_lines = _cart_items_to_promo_lines(items)
        checkout_eval = await evaluate_checkout_promotions(
            conn,
            UUID(str(tenant_id)),
            promo_lines,
            manual_discount_amount=float(discount_amount or 0),
        )
        tax_category_by_id = {line["id"]: line["tax_category"] for line in promo_lines}
        for line in checkout_eval["lines"]:
            line["tax_category"] = tax_category_by_id.get(line["id"], "standard")

        tax_config = await _get_tenant_tax_config(conn, tenant_id)
        tax_rows = _tax_rows_from_evaluated_lines(checkout_eval["lines"])
        std_tax, liq_tax, tax_label = _compute_tax_breakdown(tax_rows, tax_config)

    return {
        "standard_tax": float(std_tax),
        "liquor_tax": float(liq_tax),
        "standard_tax_label": tax_label,
        "subtotal": checkout_eval["subtotal"],
        "promo_savings": checkout_eval["promo_savings"],
        "subtotal_after_promos": checkout_eval["subtotal_after_promos"],
        "manual_discount_amount": checkout_eval.get("manual_discount_amount", 0),
        "total_amount": checkout_eval["total_amount"],
        "promo_breakdown": checkout_eval["promo_breakdown"],
        "lines": checkout_eval["lines"],
    }


async def add_order_payment(
    request: Request,
    cart_id: str,
    amount: float,
    payment_method: str,
    payment_method_id: Optional[str] = None,
    cash_received: Optional[float] = None,
    tip_amount: Optional[float] = None,
    tip_source: Optional[str] = None,
    tip_taxable: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    Add a partial payment to a POS cart's underlying order.
    Supports split payments (Mode 1: by amount, Mode 2: equal split).
    When paid_total >= total_amount, completes the order automatically.

    Issue #524: cash_received captures the bill amount handed by the customer
    (only valid for payment_method='cash'). Change due is derived as
    cash_received - amount and is not stored.
    """
    # Issue #524 — defense in depth: validate cash tender at service layer
    # so we throw a clean error before the DB CHECK constraint catches it.
    if cash_received is not None:
        if payment_method != 'cash':
            raise APIError("cash_received solo aplica a pagos en efectivo", status_code=400)
        if cash_received < amount:
            raise APIError(
                f"Efectivo recibido ({cash_received}) debe ser mayor o igual al monto a cobrar ({amount})",
                status_code=400,
            )
    validate_wallet_payment_tender(payment_method, cash_received)
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        payment_id = None
        paid_total = 0.0
        remaining = 0.0
        is_complete = False
        order_id = None
        award_customer_id = None

        async with get_db_connection() as conn:
            timezone_name = await resolve_tenant_timezone(conn, tenant_id)
            async with conn.transaction():
                # 1. Lock cart row
                cart_row = await conn.fetchrow(
                    """
                    SELECT id, tenant_id
                    FROM pos_carts
                    WHERE id = $1 AND tenant_id = $2
                    FOR UPDATE
                    """,
                    cart_id, tenant_id
                )

                if not cart_row:
                    raise APIError("Cart not found", status_code=404)

                # 2. Fetch + lock the order linked to this cart via pos_cart_id
                order_row = await conn.fetchrow(
                    """
                    SELECT id, total_amount, tip_amount, tip_source, tip_taxable,
                           tip_tax_amount, status, payment_status, customer_id,
                           order_number
                    FROM orders
                    WHERE pos_cart_id = $1 AND tenant_id = $2
                    ORDER BY created_at DESC
                    LIMIT 1
                    FOR UPDATE
                    """,
                    cart_id, tenant_id
                )

                if not order_row:
                    raise APIError("No order found for this cart — complete the order first", status_code=400)

                order_id = order_row["id"]

                if not order_row:
                    raise APIError("Order not found", status_code=404)

                if order_row["status"] == "cancelled":
                    raise APIError("Cannot add payment to a cancelled order", status_code=409)

                if order_row["status"] == "completed" and order_row["payment_status"] == "paid":
                    raise APIError("Order is already fully paid", status_code=409)

                total_amount = float(order_row["total_amount"])
                resolved_tip_amount = float(order_row["tip_amount"] or 0)
                resolved_tip_source = order_row["tip_source"] or "none"
                resolved_tip_taxable = bool(order_row["tip_taxable"])
                resolved_tip_tax_amount = float(order_row["tip_tax_amount"] or 0)

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
                        order_id,
                        resolved_tip_amount,
                        resolved_tip_source,
                        resolved_tip_taxable,
                        resolved_tip_tax_amount,
                    )

                amount_due = split_settlement_amount_due(
                    total_amount,
                    resolved_tip_amount,
                    resolved_tip_tax_amount,
                )
                paid_before_row = await conn.fetchrow(
                    """
                    SELECT COALESCE(SUM(amount), 0) AS paid_total
                    FROM order_payments
                    WHERE order_id = $1 AND voided_at IS NULL
                    """,
                    order_id,
                )
                paid_before = float(paid_before_row["paid_total"])
                remaining_before = max(0.0, amount_due - paid_before)
                if amount - remaining_before > 0.01:
                    raise APIError(
                        f"El pago excede el saldo pendiente ({remaining_before})",
                        status_code=400,
                    )

                user_id = session_context.user_id if hasattr(session_context, 'user_id') else None
                customer_uuid = order_row["customer_id"]
                if payment_method == WALLET_PAYMENT_SLUG and not customer_uuid:
                    raise APIError(
                        "La billetera requiere un cliente en la orden",
                        status_code=400,
                    )

                # 3. Insert payment record
                payment_row = await conn.fetchrow(
                    """
                    INSERT INTO order_payments
                        (order_id, tenant_id, amount, payment_method, payment_method_id, created_by_user_id, cash_received)
                    VALUES
                        ($1, $2, $3, $4, $5::uuid, $6::uuid, $7)
                    RETURNING id
                    """,
                    order_id, tenant_id, amount, payment_method,
                    payment_method_id,
                    user_id,
                    cash_received,
                )
                payment_id = str(payment_row["id"])

                if payment_method == WALLET_PAYMENT_SLUG:
                    await apply_wallet_for_order(
                        conn,
                        customer_uuid,
                        UUID(str(tenant_id)),
                        Decimal(str(amount)),
                        order_id,
                        UUID(str(user_id)) if user_id else None,
                        UUID(payment_id),
                    )

                # 4. Compute paid total (warocol.com#649: ignore voided rows)
                paid_total_row = await conn.fetchrow(
                    "SELECT COALESCE(SUM(amount), 0) AS paid_total FROM order_payments WHERE order_id = $1 AND voided_at IS NULL",
                    order_id
                )
                paid_total = float(paid_total_row["paid_total"])
                remaining = max(0.0, amount_due - paid_total)
                is_complete = remaining <= 0.01  # tolerance for rounding

                # 5. Update order status
                if is_complete:
                    award_customer_id = await conn.fetchval(
                        "SELECT customer_id FROM orders WHERE id = $1",
                        order_id,
                    )
                    await conn.execute(
                        """
                        UPDATE orders
                        SET status = 'completed',
                            payment_method = $2,
                            payment_method_id = $3::uuid,
                            payment_status = 'paid',
                            order_date = COALESCE(order_date, now())
                        WHERE id = $1
                        """,
                        order_id, payment_method,
                        payment_method_id,
                    )
                    # Mostrador: auto-deliver; barra: keep comandas open (#799).
                    _bar_order = await conn.fetchval(
                        """
                        SELECT t.is_bar
                          FROM orders o
                          JOIN table_sessions ts ON ts.id = o.table_session_id
                          JOIN tables t ON t.id = ts.table_id
                         WHERE o.id = $1 AND o.tenant_id = $2
                        """,
                        order_id,
                        tenant_id,
                    )
                    if not _bar_order:
                        try:
                            await finalize_open_comandas(conn, order_id, tenant_id)
                        except Exception as _ce:
                            logger.warning(f"Could not finalize comandas for order {order_id}: {_ce}")
                else:
                    await conn.execute(
                        "UPDATE orders SET payment_status = 'partial' WHERE id = $1",
                        order_id
                    )

                if is_complete and resolved_tip_amount > 0:
                    try:
                        order_meta = await conn.fetchrow(
                            "SELECT order_number FROM orders WHERE id = $1",
                            order_id,
                        )
                        tip_tax_config = await _get_tenant_tax_config(conn, tenant_id)
                        await _post_deferred_order_tip_gl(
                            conn=conn,
                            tenant_id=tenant_id,
                            order_id=order_id,
                            tip_amount=Decimal(str(resolved_tip_amount)),
                            tip_tax_amount=Decimal(str(resolved_tip_tax_amount)),
                            payment_method=payment_method,
                            payment_method_id=UUID(payment_method_id) if payment_method_id else None,
                            tax_config=tip_tax_config,
                            order_number=int(order_meta["order_number"]) if order_meta else None,
                        )
                    except Exception as _tip_gl_err:
                        logger.error(
                            f"Deferred tip GL failed for POS order {order_id}: {_tip_gl_err}"
                        )

                if is_complete:
                    try:
                        order_meta = await conn.fetchrow(
                            """
                            SELECT order_number, order_date, total_amount
                            FROM orders
                            WHERE id = $1
                            """,
                            order_id,
                        )
                        tax_config = await _get_tenant_tax_config(conn, tenant_id)
                        await _post_order_gl_entry(
                            conn=conn,
                            tenant_id=tenant_id,
                            order_id=order_id,
                            order_date=local_date_for_tenant(order_meta["order_date"], timezone_name),
                            total_amount=Decimal(str(order_meta["total_amount"])),
                            payment_method=payment_method,
                            payment_method_id=UUID(payment_method_id) if payment_method_id else None,
                            tax_config=tax_config,
                            order_number=int(order_meta["order_number"]) if order_meta else None,
                            payment_splits=await _order_payment_splits_for_gl(conn, order_id),
                        )
                    except Exception as _gl_err:
                        logger.error(f"Split payment GL failed for POS order {order_id}: {_gl_err}")

        # 6. Fire-and-forget side effects OUTSIDE transaction (only on completion)
        if is_complete and award_customer_id:
            try:
                asyncio.create_task(
                    evaluate_and_award(order_id, award_customer_id, tenant_id)
                )
            except Exception as _w_err:
                logger.warning(f"Could not schedule waros for order {order_id}: {_w_err}")

        return {
            "success": True,
            "data": {
                "payment_id": payment_id,
                "paid_total": paid_total,
                "remaining": remaining,
                "is_complete": is_complete,
                "tip_amount": resolved_tip_amount,
                "tip_source": resolved_tip_source,
                "tip_taxable": resolved_tip_taxable,
                "tip_tax_amount": resolved_tip_tax_amount,
                "order_id": str(order_id),
                "order_number": int(order_row["order_number"]),
                "total_amount": total_amount,
                "charged_amount": amount_due,
                "payment_method": payment_method,
                "payment_status": "paid" if is_complete else "partial",
                "status": "completed" if is_complete else order_row["status"],
            }
        }

    except (AuthenticationError, APIError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error adding order payment: {str(e)}")
        raise APIError(f"Error adding payment: {str(e)}", status_code=500)


# Roles allowed to void another cashier's payment. The original creator can
# always void their own. Issue warocol.com#649.
_PAYMENT_VOID_ROLES = {'admin', 'superuser'}


async def void_order_payment(
    request: Request,
    cart_id: str,
    payment_id: str,
    reason: Optional[str] = None,
    channel: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Issue warocol.com#649 — soft-delete a partial payment on a POS cart's order.

    - Marks order_payments.voided_at = NOW().
    - Recomputes paid_total ignoring voided rows.
    - Reopens the order (payment_status='partial') and the cart
      (status='active') when the voided row was the one that closed them.
    - Auto-reverses the posted sale journal entry in the same transaction.

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
                # 1. Lock the payment row and join to its parent cart/order.
                payment_row = await conn.fetchrow(
                    """
                    SELECT op.id, op.order_id, op.amount, op.payment_method,
                           op.cash_received, op.created_by_user_id, op.voided_at,
                           op.payment_method_id,
                           o.pos_cart_id, o.total_amount, o.tip_amount, o.tip_tax_amount,
                           o.payment_status, o.status AS order_status, o.customer_id
                    FROM order_payments op
                    JOIN orders o ON o.id = op.order_id
                    WHERE op.id = $1::uuid AND op.tenant_id = $2
                    FOR UPDATE OF op
                    """,
                    payment_id, tenant_id,
                )
                if not payment_row:
                    raise APIError("Pago no encontrado", status_code=404)
                if payment_row["voided_at"] is not None:
                    raise APIError("Este pago ya fue anulado", status_code=409)
                # pos_cart_id is NULL on mesa orders — flag the wrong endpoint
                # instead of returning the generic mismatch error.
                if payment_row["pos_cart_id"] is None:
                    raise APIError(
                        "Este pago pertenece a una sesión de mesa — usa el endpoint /api/tables/{table_id}/payments/{payment_id}",
                        status_code=400,
                    )
                if str(payment_row["pos_cart_id"]) != str(cart_id):
                    raise APIError("El pago no pertenece a este carrito", status_code=400)

                order_id = payment_row["order_id"]

                # 2. Authorization: creator or manager-level role.
                creator_id = payment_row["created_by_user_id"]
                if creator_id is not None and str(creator_id) != str(user_id) and role not in _PAYMENT_VOID_ROLES:
                    raise APIError(
                        "Solo el cajero que registró el pago o un administrador puede anularlo",
                        status_code=403,
                    )

                # 3. Lock the parent order to coordinate concurrent voids.
                await conn.execute("SELECT 1 FROM orders WHERE id = $1 FOR UPDATE", order_id)

                was_paid = payment_row["payment_status"] == "paid"

                # 4. Mark payment as voided (soft delete).
                await conn.execute(
                    "UPDATE order_payments SET voided_at = NOW(), void_reason = $2 WHERE id = $1",
                    payment_row["id"],
                    normalized_reason,
                )
                wallet_restore_movement_id = None
                if payment_row["payment_method"] == WALLET_PAYMENT_SLUG:
                    if not payment_row["customer_id"]:
                        raise APIError(
                            "La billetera requiere un cliente en la orden",
                            status_code=400,
                        )
                    wallet_restore_movement_id = await restore_wallet_for_order_payment_void(
                        conn,
                        payment_row["customer_id"],
                        UUID(str(tenant_id)),
                        Decimal(str(payment_row["amount"])),
                        order_id,
                        UUID(str(payment_row["id"])),
                        UUID(str(user_id)) if user_id else None,
                        notes=f"Anulación pago parcial: {normalized_reason}",
                    )

                # 5. Recompute paid total ignoring voided rows.
                paid_row = await conn.fetchrow(
                    "SELECT COALESCE(SUM(amount), 0) AS paid FROM order_payments WHERE order_id = $1 AND voided_at IS NULL",
                    order_id,
                )
                paid_total = float(paid_row["paid"])
                total_amount = float(payment_row["total_amount"])
                amount_due = split_settlement_amount_due(
                    total_amount,
                    float(payment_row["tip_amount"] or 0),
                    float(payment_row["tip_tax_amount"] or 0),
                )
                remaining = max(0.0, amount_due - paid_total)
                is_complete = remaining <= 0.01

                # 6. If voiding flipped the order out of fully-paid, reopen.
                reopened = was_paid and not is_complete
                if reopened:
                    await conn.execute(
                        "UPDATE orders SET payment_status = 'partial' WHERE id = $1",
                        order_id,
                    )
                    await conn.execute(
                        "UPDATE pos_carts SET status = 'active', updated_at = NOW() WHERE id = $1",
                        payment_row["pos_cart_id"],
                    )
                    # Reverse the posted GL entry — same transaction, atomic.
                    await void_order_journal_entry_in_txn(
                        conn, tenant_id, order_id, user_id, normalized_reason,
                    )

                await record_operation_event(
                    conn,
                    tenant_id,
                    domain=DOMAIN_POS,
                    channel=_normalize_cart_channel(channel),
                    action="payment_voided",
                    actor_user_id=user_id,
                    pos_cart_id=payment_row["pos_cart_id"],
                    order_id=order_id,
                    reason=normalized_reason,
                    payload={
                        "voided_ids": [str(payment_id)],
                        "order_ids": [str(order_id)],
                        "payment_method": payment_row["payment_method"],
                        "amount": float(payment_row["amount"]),
                        "cash_received": (
                            float(payment_row["cash_received"])
                            if payment_row["cash_received"] is not None
                            else None
                        ),
                        "wallet_restore_movement_id": (
                            str(wallet_restore_movement_id)
                            if wallet_restore_movement_id
                            else None
                        ),
                        "reopened": reopened,
                    },
                )

        logger.info(
            f"Payment {payment_id} voided (order={order_id}, paid_total={paid_total}, reopened={reopened})"
        )
        return {
            "success": True,
            "data": {
                "voided_ids": [str(payment_id)],
                "paid_total": paid_total,
                "remaining": remaining,
                "is_complete": is_complete,
                "reopened": reopened,
                "wallet_restore_movement_id": (
                    str(wallet_restore_movement_id)
                    if wallet_restore_movement_id
                    else None
                ),
            },
        }

    except (AuthenticationError, APIError):
        raise
    except Exception as e:
        logger.error(f"Error voiding payment {payment_id}: {e}")
        raise APIError(f"Error al anular el pago: {e}", status_code=500)


async def _send_tracked_pos_receipt_email(
    *,
    customer_email: str,
    order_id: UUID,
    tenant_id: UUID,
    order_number: int,
    total_amount: float,
    payment_method: str,
    items: List[dict],
    order_date: Any,
    business_name: Optional[str],
    business_address: Optional[str],
    business_city: Optional[str],
    business_phone: Optional[str],
    discount_amount: float,
    subtotal: float,
    promo_savings: float,
    promo_breakdown: List[dict],
    waro_redemption_summary: Optional[Dict[str, Any]],
    tip_amount: float,
) -> bool:
    """Fire-and-forget receipt send with optional delivery tracking (#1769). Fail-open."""
    delivery_id = None
    pixel_url = None
    try:
        tracking_token = invoice_email_tracking_service.generate_tracking_token()
        tracking_token_hash = invoice_email_tracking_service.hash_tracking_token(tracking_token)
        delivery_id = await invoice_email_tracking_service.create_pending_delivery(
            tenant_id=tenant_id,
            order_id=order_id,
            recipient_email=customer_email,
            tracking_token_hash=tracking_token_hash,
        )
        if delivery_id is not None:
            pixel_url = invoice_email_tracking_service.build_pixel_url(tracking_token)
    except Exception as track_err:
        logger.warning(f"POS receipt tracking skipped for order {order_id}: {track_err}")
        delivery_id = None
        pixel_url = None

    success = await send_pos_receipt_email(
        customer_email=customer_email,
        order_number=order_number,
        total_amount=total_amount,
        payment_method=payment_method,
        items=items,
        order_date=order_date,
        tenant_id=str(tenant_id),
        business_name=business_name,
        business_address=business_address,
        business_city=business_city,
        business_phone=business_phone,
        discount_amount=discount_amount,
        subtotal=subtotal,
        promo_savings=promo_savings,
        promo_breakdown=promo_breakdown,
        waro_redemption_summary=waro_redemption_summary,
        tip_amount=tip_amount,
        tracking_pixel_url=pixel_url,
    )

    if delivery_id is not None:
        try:
            if success:
                await invoice_email_tracking_service.mark_delivery_sent(delivery_id)
            else:
                await invoice_email_tracking_service.mark_delivery_failed(
                    delivery_id, failure_code="ses_rejected"
                )
        except Exception as mark_err:
            logger.warning(f"POS receipt delivery status update failed: {mark_err}")

    return bool(success)


async def complete_pos_order(
    request: Request,
    cart_id: UUID,
    payment_method: Optional[str],
    customer_id: UUID,
    credit_due_date: Optional[date] = None,
    payment_method_id: Optional[UUID] = None,
    receipt_email: Optional[str] = None,
    discount_type: Optional[str] = None,
    discount_value: Optional[float] = None,
    split_mode: bool = False,
    split_first_amount: float = 0.0,
    table_session_id: Optional[UUID] = None,
    delivery_address_id: Optional[UUID] = None,
    scheduled_time: Optional[datetime] = None,
    delivery_instructions: Optional[str] = None,
    *,
    split_first_cash_received: Optional[float] = None,
    cash_received: Optional[float] = None,
    served_by_member_id: Optional[UUID] = None,
    tip_amount: float = 0,
    tip_source: str = 'none',
    tip_taxable: bool = False,
    waros_to_redeem: Optional[int] = None,
    waro_reward_id: Optional[UUID] = None,
) -> dict:
    """
    Complete a POS order.

    Manual checkout contract:
    automatic promotions -> manual discount -> WaRo redemption -> payment
    tender. Wallet is recorded as payment_method='customer_wallet', not as a
    discount. Split payments create order_payments rows; orders.payment_method
    remains a legacy/single-payment compatibility field.

    Flow:
    1. Associate customer with cart
    2. Create order record
    3. Copy cart items to order_items
    4. Copy modifiers to order_item_modifiers
    5. Mark cart as completed
    6. Update inventory if product controls stock
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        # warocol.com#575 + warocol.com#637 — Resolve served_by_member_id and the
        # tipping gate before the main transaction. Combined into a single SELECT
        # to keep the round-trip count identical to the pre-tipping code path.
        # Member existence is only checked when waiter attribution is requested.
        resolved_served_by: Optional[UUID] = None
        waiter_attribution_enabled = False
        tip_enabled = False
        if served_by_member_id is not None or tip_amount > 0 or tip_source != 'none':
            async with get_db_connection(use_transaction=False) as _conn:
                flags = await _conn.fetchrow(
                    "SELECT waiter_attribution_enabled, tip_enabled FROM tenant_public_profiles WHERE tenant_id = $1",
                    tenant_id,
                )
                if flags:
                    waiter_attribution_enabled = bool(flags['waiter_attribution_enabled'])
                    tip_enabled = bool(flags['tip_enabled'])
                # warocol.com#663 — persist checkout attribution even when per-table toggle is off
                if served_by_member_id is not None:
                    member_check = await _conn.fetchval(
                        """
                        SELECT id FROM tenant_members
                        WHERE id = $1 AND tenant_id = $2 AND is_active = true AND terminated_at IS NULL
                        """,
                        served_by_member_id,
                        tenant_id,
                    )
                    if member_check is None:
                        raise NotFoundError("Member not found")
                    resolved_served_by = served_by_member_id

        # warocol.com#637 — tip validation (fail fast, before the transaction).
        try:
            tip_amount, tip_source, _tip_taxable = normalize_tip_payload(
                tip_amount, tip_source, tip_taxable,
            )
        except ValueError as exc:
            raise APIError(str(exc), status_code=400)
        if tip_amount > 0 and not tip_enabled:
            raise APIError("Tipping is not enabled for this tenant", status_code=400)

        _is_bar_sale = False
        _bar_display_name: Optional[str] = None

        async with get_db_connection() as conn:
            async with conn.transaction():
                pending_without_payment = payment_method is None
                if pending_without_payment:
                    if delivery_address_id is None:
                        raise APIError("payment_method es requerido para ventas que no son domicilio", status_code=400)
                    if split_mode:
                        raise APIError("El cobro dividido requiere método de pago", status_code=400)
                    if payment_method_id is not None:
                        raise APIError("payment_method_id no aplica cuando la venta queda pendiente", status_code=400)
                    if credit_due_date is not None:
                        raise APIError("credit_due_date requiere método de pago a crédito", status_code=400)
                    if cash_received is not None or split_first_cash_received is not None:
                        raise APIError("El efectivo recibido requiere método de pago en efectivo", status_code=400)

                # 0. Backend guard: credit / wallet require an identified (non-anonymous) customer
                if payment_method in ('credit', WALLET_PAYMENT_SLUG):
                    customer_check = await conn.fetchrow(
                        "SELECT phone_number FROM profile WHERE id = $1",
                        customer_id
                    )
                    if customer_check and customer_check['phone_number'] == '0000000000':
                        raise APIError(
                            "El pago a crédito o billetera requiere un cliente identificado (no anónimo)",
                            status_code=400
                        )
                validate_wallet_payment_tender(payment_method, cash_received)

                # 0b. Delivery validation: address ownership + soft-delete + non-anonymous customer
                if delivery_address_id is not None:
                    delivery_customer_check = await conn.fetchrow(
                        "SELECT phone_number FROM profile WHERE id = $1",
                        customer_id
                    )
                    if delivery_customer_check and delivery_customer_check['phone_number'] == '0000000000':
                        raise APIError(
                            "El domicilio requiere un cliente identificado (no anónimo)",
                            status_code=400
                        )
                    address_ok = await conn.fetchval(
                        "SELECT 1 FROM addresses_profile WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL",
                        delivery_address_id,
                        customer_id,
                    )
                    if not address_ok:
                        raise APIError(
                            "Dirección de entrega no válida o no pertenece al cliente",
                            status_code=400
                        )

                # 1. Verify cart exists and is active
                cart_query = """
                    SELECT id, customer_id, total_amount, tenant_id
                    FROM pos_carts
                    WHERE id = $1 AND status = 'active'
                """
                cart_row = await conn.fetchrow(cart_query, cart_id)

                if not cart_row:
                    raise APIError("Cart not found or already completed", status_code=404)

                # 1b. Associate customer with cart (update if different or null)
                if cart_row['customer_id'] != customer_id:
                    await conn.execute(
                        "UPDATE pos_carts SET customer_id = $1 WHERE id = $2",
                        customer_id,
                        cart_id
                    )
                    logger.info(f"Associated customer {customer_id} with cart {cart_id}")

                # 2. Get cart items
                items = await get_cart_items(conn, cart_id)

                if not items:
                    raise APIError("Cannot complete order with empty cart", status_code=400)

                tax_config = await _get_tenant_tax_config(conn, tenant_id)
                _tip_taxable = bool(tip_taxable) if tip_amount > 0 else False
                _tip_tax_amount = compute_tip_tax_amount(
                    float(tip_amount), _tip_taxable, tax_config,
                )

                # 3. Create order record (use customer_id from parameter)
                # Compute order/payment status. Delivery orders can be dispatched
                # before payment is known; accounting is posted only when later
                # finalized from /ventas/:id.
                order_status = 'pending' if pending_without_payment else 'completed'
                if pending_without_payment:
                    payment_status = None
                elif split_mode:
                    payment_status = 'partial'
                elif payment_method == 'credit':
                    payment_status = 'credit'
                else:
                    payment_status = 'paid'

                # Compute promotions + manual discount (batch #982 — promos first)
                from app.services.promotions_service import evaluate_checkout_promotions

                promo_lines = _cart_items_to_promo_lines(items)
                checkout_eval = await evaluate_checkout_promotions(
                    conn,
                    UUID(str(tenant_id)),
                    promo_lines,
                    discount_type=discount_type,
                    discount_value=discount_value,
                )

                from app.services.waros_service import (
                    apply_checkout_waro_redemption,
                    settle_waro_redemption,
                )
                from fastapi import HTTPException as FastAPIHTTPException

                try:
                    checkout_eval = await apply_checkout_waro_redemption(
                        conn,
                        tenant_id,
                        customer_id,
                        checkout_eval,
                        waros_to_redeem=waros_to_redeem,
                        waro_reward_id=waro_reward_id,
                    )
                except FastAPIHTTPException as waro_exc:
                    raise APIError(waro_exc.detail, status_code=waro_exc.status_code)

                _waro_preview = checkout_eval.pop("_waro_redemption_preview", None)

                cart_subtotal = float(checkout_eval["subtotal"])
                _promo_savings = float(checkout_eval["promo_savings"])
                _subtotal_after_promos = float(checkout_eval["subtotal_after_promos"])
                _discount_amount = checkout_eval.get("manual_discount_amount") or None
                _discounted_total = float(checkout_eval["total_amount"])
                _promo_breakdown = checkout_eval.get("promo_breakdown") or []
                _eval_by_id = {line["id"]: line for line in checkout_eval["lines"]}

                # Issue #524 — single-payment cash flow stores cash_received on the orders row.
                # Split mode keeps it NULL here and stores per-line on order_payments below (step 7a).
                # warocol.com#637 — when tipping is in effect, cash_received must cover
                # total + tip (the customer must hand over enough physical cash for both).
                _orders_cash_received: Optional[float] = None
                if not split_mode and cash_received is not None:
                    if payment_method != 'cash':
                        raise APIError("cash_received solo aplica a pagos en efectivo", status_code=400)
                    _required_cash = float(_discounted_total) + tip_settlement_total(
                        float(tip_amount), _tip_tax_amount,
                    )
                    if cash_received < _required_cash:
                        if tip_amount > 0:
                            raise APIError(
                                f"Efectivo recibido ({cash_received}) debe ser mayor o igual al total + propina"
                                f" (+ IVA propina si aplica) ({_required_cash})",
                                status_code=400,
                            )
                        raise APIError(
                            f"Efectivo recibido ({cash_received}) debe ser mayor o igual al total ({_discounted_total})",
                            status_code=400,
                        )
                    _orders_cash_received = cash_received

                order_query = """
                    INSERT INTO orders (
                        user_id, tenant_id, customer_id, payment_method, pos_cart_id,
                        order_date, total_amount, status, payment_status, credit_due_date,
                        payment_method_id, discount_type, discount_value, discount_amount,
                        table_session_id,
                        delivery_address_id, scheduled_time, delivery_instructions,
                        cash_received,
                        served_by_member_id,
                        tip_amount, tip_source,
                        tip_taxable, tip_tax_amount
                    )
                    VALUES ($1, $2, $3, $4, $5, NOW(), $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23)
                    RETURNING id, order_number, created_at
                """
                order_row = await conn.fetchrow(
                    order_query,
                    user_id,
                    tenant_id,
                    customer_id,  # Use parameter instead of cart_row
                    payment_method,
                    cart_id,
                    _discounted_total,
                    order_status,
                    payment_status,
                    credit_due_date,
                    payment_method_id,
                    discount_type,
                    discount_value,
                    _discount_amount,
                    table_session_id,
                    delivery_address_id,
                    scheduled_time,
                    delivery_instructions,
                    _orders_cash_received,
                    resolved_served_by,
                    float(tip_amount),
                    tip_source,
                    _tip_taxable,
                    float(_tip_tax_amount),
                )
                order_id = order_row['id']
                order_number = order_row['order_number']

                if _waro_preview:
                    try:
                        await settle_waro_redemption(
                            conn,
                            tenant_id,
                            customer_id,
                            order_id,
                            _waro_preview,
                        )
                    except FastAPIHTTPException as waro_exc:
                        raise APIError(waro_exc.detail, status_code=waro_exc.status_code)

                logger.info(f"Created order #{order_number} from cart {cart_id}")

                if not split_mode and payment_method == WALLET_PAYMENT_SLUG:
                    wallet_due = Decimal(str(split_settlement_amount_due(
                        float(_discounted_total),
                        float(tip_amount),
                        float(_tip_tax_amount),
                    )))
                    await apply_wallet_for_order(
                        conn,
                        customer_id,
                        UUID(str(tenant_id)),
                        wallet_due,
                        order_id,
                        UUID(str(user_id)) if user_id else None,
                    )

                # 3b. Bar session rotation: when this POS sale was attached to a bar
                # table session, close it and open a new one so the next entry shows a
                # clean tab. Mirrors the rotation logic in tables_service.close_session
                # for the mesa-as-bar path. Stays inside the existing transaction so
                # the rotation is atomic with the order persistence.
                new_table_session_id: Optional[UUID] = None
                if table_session_id is not None:
                    bar_check = await conn.fetchrow(
                        """
                        SELECT t.id AS table_id, t.is_bar
                          FROM table_sessions ts
                          JOIN tables t ON t.id = ts.table_id
                         WHERE ts.id = $1 AND ts.tenant_id = $2
                        """,
                        table_session_id, tenant_id,
                    )
                    if bar_check and bar_check['is_bar']:
                        _is_bar_sale = True
                        _bar_display_name = await conn.fetchval(
                            "SELECT name FROM tables WHERE id = $1 AND tenant_id = $2",
                            bar_check['table_id'],
                            tenant_id,
                        ) or 'Barra'
                        await conn.execute(
                            "UPDATE table_sessions SET closed_at = now() WHERE id = $1",
                            table_session_id,
                        )
                        new_session_row = await conn.fetchrow(
                            """
                            INSERT INTO table_sessions (table_id, tenant_id, opened_by_user_id)
                            VALUES ($1, $2, NULL)
                            RETURNING id
                            """,
                            bar_check['table_id'], tenant_id,
                        )
                        new_table_session_id = new_session_row['id']
                        logger.info(
                            f"Bar session rotated on POS complete: closed {table_session_id}, "
                            f"opened {new_table_session_id} for table {bar_check['table_id']}"
                        )

                # 4. Copy cart items to order_items
                from app.services.promotions_service import promo_persist_fields_from_eval_line

                for i, item in enumerate(items):
                    eval_line = _eval_by_id.get(item["id"], {})
                    total_alloc = int(eval_line.get("total_discount_allocated") or 0)
                    _da = total_alloc if total_alloc > 0 else None
                    _nt = eval_line.get("net_total") if total_alloc > 0 else None
                    _promo_id, _promo_savings = promo_persist_fields_from_eval_line(eval_line)
                    # Insert order item
                    _item_notes = (item.get('notes') or '').strip() or None
                    order_item_query = """
                        INSERT INTO order_items (
                            order_id, product_id, quantity, price_at_purchase, subtotal,
                            discount_allocated, net_total, notes,
                            applied_promotion_id, promo_savings_allocated
                        )
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                        RETURNING id
                    """
                    order_item_row = await conn.fetchrow(
                        order_item_query,
                        order_id,
                        item['product']['id'],
                        item['quantity'],
                        item['product']['price'],
                        item['subtotal'],
                        _da,
                        _nt,
                        _item_notes,
                        _promo_id,
                        _promo_savings,
                    )
                    order_item_id = order_item_row['id']

                    # 5. Copy modifiers to order_item_modifiers and deduct ingredient inventory
                    if item['modifiers']:
                        for modifier in item['modifiers']:
                            # Get modifier quantity from cart (default 1)
                            modifier_qty = modifier.get('quantity', 1)

                            modifier_query = """
                                INSERT INTO order_item_modifiers (
                                    order_item_id, modifier_id, modifier_name,
                                    price_at_purchase, quantity,
                                    included_quantity_at_purchase
                                )
                                VALUES ($1, $2, $3, $4, $5, $6)
                            """
                            await conn.execute(
                                modifier_query,
                                order_item_id,
                                modifier['id'],
                                modifier['name'],
                                modifier['price'],
                                modifier_qty,
                                modifier.get("included_quantity", 0),
                            )

                            await _deduct_modifier_inventory_for_order_item(
                                conn,
                                tenant_id=tenant_id,
                                user_id=session_context.user_id,
                                order_id=order_id,
                                order_item_id=order_item_id,
                                order_number=order_number,
                                item_quantity=float(item['quantity']),
                                modifier=modifier,
                                modifier_qty=float(modifier_qty),
                            )

                    # 6. ALWAYS update inventory for all products (no controla_stock check)
                    # Get ALL ingredients for this product:
                    # - Direct ingredients from product_recipes
                    # - Ingredients from recipe bases (product_base_recipes → base_recipe_templates)
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
                            brt.base_quantity * pbr.quantity AS quantity,
                            brt.unit,
                            i.name as ingredient_name
                        FROM product_base_recipes pbr
                        JOIN base_recipe_templates brt ON pbr.product_base_type_id = brt.product_base_type_id
                        JOIN ingredients i ON brt.ingredient_id = i.id
                        WHERE pbr.product_id = $1
                    """
                    ingredients = await conn.fetch(ingredients_query, item['product']['id'])

                    # Reduce inventory for each ingredient
                    for ingredient in ingredients:
                        # Apply gr → und conversion if recipe unit differs from ingredient base unit
                        resolved_qty = await resolve_recipe_quantity_to_base_unit(
                            conn,
                            ingredient["ingredient_id"],
                            float(ingredient["quantity"]),
                            ingredient["unit"] or "",
                        )
                        quantity_to_deduct = float(item['quantity']) * resolved_qty

                        # Get current stock before update
                        current_stock_query = """
                            SELECT current_stock
                            FROM tenant_inventory
                            WHERE ingredient_id = $1 AND tenant_id = $2
                        """
                        stock_row = await conn.fetchrow(
                            current_stock_query,
                            ingredient['ingredient_id'],
                            tenant_id
                        )

                        previous_stock = float(stock_row['current_stock']) if stock_row else 0.0
                        new_stock = previous_stock - quantity_to_deduct

                        # Update inventory
                        if stock_row:
                            update_inventory_query = """
                                UPDATE tenant_inventory
                                SET current_stock = $1, last_updated = NOW()
                                WHERE ingredient_id = $2 AND tenant_id = $3
                            """
                            await conn.execute(
                                update_inventory_query,
                                new_stock,
                                ingredient['ingredient_id'],
                                tenant_id
                            )
                        else:
                            # Create inventory record if it doesn't exist (negative stock)
                            insert_inventory_query = """
                                INSERT INTO tenant_inventory (
                                    tenant_id, ingredient_id, current_stock, minimum_stock, last_updated
                                )
                                VALUES ($1, $2, $3, 0, NOW())
                            """
                            await conn.execute(
                                insert_inventory_query,
                                tenant_id,
                                ingredient['ingredient_id'],
                                -quantity_to_deduct
                            )

                        # Create movement record for consumption
                        movement_query = """
                            INSERT INTO tenant_ingredient_movements (
                                tenant_id,
                                ingredient_id,
                                movement_type,
                                quantity_change,
                                unit,
                                previous_stock,
                                new_stock,
                                reference_table,
                                reference_id,
                                reason,
                                created_by,
                                created_at
                            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW())
                        """
                        await conn.execute(
                            movement_query,
                            tenant_id,
                            ingredient['ingredient_id'],
                            'consumption',
                            -quantity_to_deduct,
                            ingredient['unit'],
                            previous_stock,
                            new_stock,
                            'orders',
                            order_id,
                            f"Venta de {item['quantity']}x {item['product']['name']} - Orden #{order_number}",
                            session_context.user_id
                        )

                        logger.info(
                            f"Inventory deducted: {ingredient['ingredient_name']} "
                            f"-{quantity_to_deduct}{ingredient['unit']} "
                            f"(Order #{order_number})"
                        )

                    # 6b. Capture ingredient snapshot for order_item_ingredients
                    await _capture_order_item_ingredients(
                        conn, order_item_id, item['product']['id'],
                        float(item['quantity']), tenant_id
                    )

                # Auto-fire hook for POS counter (direct checkout)
                try:
                    _prof = await conn.fetchrow(
                        "SELECT comandas_enabled FROM tenant_public_profiles WHERE tenant_id = $1",
                        tenant_id
                    )
                    if _prof and _prof["comandas_enabled"]:
                        _is_delivery = delivery_address_id is not None
                        if _is_delivery:
                            _fire_source, _fire_label = 'delivery', f'Domicilio #{order_number}'
                        elif _is_bar_sale:
                            _fire_source, _fire_label = 'table', _bar_display_name or 'Barra'
                        else:
                            _fire_source, _fire_label = 'pos', 'Mostrador'
                        await fire_comandas(
                            order_id=order_id,
                            tenant_id=tenant_id,
                            source_type=_fire_source,
                            table_display_name=_fire_label,
                            conn=conn
                        )
                except Exception as _fe:
                    logger.error(f"Auto-fire failed for POS order {order_id}: {_fe}")

                # 7a. In split mode: record first payment; cart stays active.
                # Issue #524: split_first_cash_received is captured when the first split is cash.
                _split_paid_total = 0.0
                _split_remaining = float(_discounted_total)
                _split_is_complete = False
                _split_first_payment_id: Optional[str] = None
                if split_mode and split_first_amount > 0:
                    if split_first_cash_received is not None:
                        if payment_method != 'cash':
                            raise APIError("split_first_cash_received solo aplica a pagos en efectivo", status_code=400)
                        if split_first_cash_received < split_first_amount:
                            raise APIError(
                                f"Efectivo recibido ({split_first_cash_received}) debe ser mayor o igual al monto ({split_first_amount})",
                                status_code=400,
                            )
                    _amount_due = split_settlement_amount_due(
                        float(_discounted_total), float(tip_amount), _tip_tax_amount,
                    )
                    if split_first_amount - _amount_due > 0.01:
                        raise APIError(
                            f"El pago excede el saldo pendiente ({_amount_due})",
                            status_code=400,
                        )
                    # Issue warocol.com#649 — RETURNING id so the frontend has a
                    # real UUID for void operations (was: None → placeholder → 422).
                    _split_first_payment_row = await conn.fetchrow(
                        """
                        INSERT INTO order_payments
                            (order_id, tenant_id, amount, payment_method, payment_method_id, created_by_user_id, cash_received)
                        VALUES ($1, $2, $3, $4, $5::uuid, $6::uuid, $7)
                        RETURNING id
                        """,
                        order_id, tenant_id, split_first_amount, payment_method,
                        str(payment_method_id) if payment_method_id else None,
                        str(user_id) if user_id else None,
                        split_first_cash_received,
                    )
                    _split_first_payment_id = str(_split_first_payment_row["id"])
                    if payment_method == WALLET_PAYMENT_SLUG:
                        await apply_wallet_for_order(
                            conn,
                            customer_id,
                            UUID(str(tenant_id)),
                            Decimal(str(split_first_amount)),
                            order_id,
                            UUID(str(user_id)) if user_id else None,
                            UUID(_split_first_payment_id),
                        )
                    _split_paid_total = split_first_amount
                    _split_remaining = max(0.0, _amount_due - split_first_amount)
                    _split_is_complete = _split_remaining <= 0.01

                # 7b. Mark cart completed — skip in split mode unless fully paid
                if not split_mode or _split_is_complete:
                    await conn.execute(
                        "UPDATE pos_carts SET status = 'completed', updated_at = NOW() WHERE id = $1",
                        cart_id
                    )
                    if _split_is_complete:
                        await conn.execute(
                            "UPDATE orders SET payment_status = 'paid' WHERE id = $1",
                            order_id
                        )
                    # Mostrador/delivery: auto-deliver after checkout. Barra: kitchen
                    # closes comandas manually (warocol.com#799).
                    if order_status == 'completed' and not _is_bar_sale:
                        try:
                            await finalize_open_comandas(conn, order_id, tenant_id)
                        except Exception as _ce:
                            logger.warning(f"Could not finalize comandas for order {order_id}: {_ce}")

                logger.info(f"Order #{order_number} created (split_mode={split_mode})")

                # Tax config — fetched once, used for GL entry and receipt breakdown
                tax_config = await _get_tenant_tax_config(conn, tenant_id)
                timezone_name = await resolve_tenant_timezone(conn, tenant_id)

                if order_status == 'completed' and (not split_mode or _split_is_complete):
                    # GL journal entry — failure never blocks order completion
                    try:
                        _gl_tip = Decimal("0")
                        _gl_tip_tax = Decimal("0")
                        if not split_mode or _split_is_complete:
                            _gl_tip = Decimal(str(tip_amount))
                            _gl_tip_tax = Decimal(str(_tip_tax_amount))
                        await _post_order_gl_entry(
                            conn=conn,
                            tenant_id=tenant_id,
                            order_id=order_id,
                            order_date=local_date_for_tenant(order_row['created_at'], timezone_name),
                            total_amount=Decimal(str(_discounted_total)),
                            payment_method=payment_method,
                            payment_method_id=payment_method_id,
                            tax_config=tax_config,
                            order_number=int(order_number),
                            tip_amount=_gl_tip,
                            tip_tax_amount=_gl_tip_tax,
                            payment_splits=await _order_payment_splits_for_gl(conn, order_id) if split_mode else None,
                        )
                    except Exception as e:
                        logger.error(f"GL entry failed for POS order {order_id}: {e}")
                        # Do NOT re-raise — order completes regardless

                    # COGS GL entry — DR 6135 Costo de ventas / CR 1435 Inventarios
                    try:
                        await _post_order_cogs_gl_entry(
                            conn=conn,
                            tenant_id=tenant_id,
                            order_id=order_id,
                            order_date=local_date_for_tenant(order_row['created_at'], timezone_name),
                            order_number=int(order_number),
                        )
                    except Exception as e:
                        logger.error(f"COGS GL entry failed for POS order {order_id}: {e}")

                # Compute tax breakdown for receipt display (same engine as cart/orders)
                _standard_tax = 0.0
                _liquor_tax = 0.0
                _standard_tax_label = "Impuesto"
                try:
                    from app.services.orders_service import _compute_tax_breakdown

                    items_tax_rows = await conn.fetch(
                        """SELECT COALESCE(p.tax_category, 'standard') AS tax_category,
                                  COALESCE(oi.subtotal, 0) AS subtotal
                           FROM order_items oi
                           JOIN product p ON p.id = oi.product_id
                           WHERE oi.order_id = $1""",
                        order_id
                    )
                    _standard_tax, _liquor_tax, _standard_tax_label = _compute_tax_breakdown(
                        items_tax_rows, tax_config,
                    )
                except Exception as e:
                    logger.warning(f"Tax breakdown computation failed for order {order_id}: {e}")

                _waro_redemption_summary = await _get_order_waro_redemption_summary(conn, order_id)

                # Capture values needed after the transaction closes
                _order_id = order_id
                _customer_id = customer_id
                _tenant_id = tenant_id
                _items = items
                _order_date = order_row['created_at']
                _order_number = int(order_number)
                _total_amount = float(_discounted_total)
                _order_status = order_status
                _result = {
                    "success": True,
                    "message": "Order saved as pending" if order_status == 'pending' else "Order completed successfully",
                    "data": {
                        "order_id": str(order_id),
                        "order_number": int(order_number),
                        "total_amount": float(_discounted_total),
                        "tip_amount": float(tip_amount),
                        "tip_source": tip_source,
                        "tip_taxable": _tip_taxable,
                        "tip_tax_amount": float(_tip_tax_amount),
                        "charged_amount": float(_discounted_total) + tip_settlement_total(
                            float(tip_amount), _tip_tax_amount,
                        ),
                        "payment_method": payment_method,
                        "payment_status": payment_status,
                        "status": order_status,
                        "credit_due_date": str(credit_due_date) if credit_due_date is not None else None,
                        "items_count": len(items),
                        "created_at": order_row['created_at'].isoformat(),
                        "standard_tax": _standard_tax,
                        "liquor_tax": _liquor_tax,
                        "standard_tax_label": _standard_tax_label,
                        "next_table_session_id": str(new_table_session_id) if new_table_session_id else None,
                        "subtotal": cart_subtotal,
                        "promo_savings": _promo_savings,
                        "promo_breakdown": _promo_breakdown,
                        "waro_redemption_summary": _waro_redemption_summary,
                        "discount_amount": float(_discount_amount) if _discount_amount else 0.0,
                        # Split mode extras
                        **({"paid_total": _split_paid_total, "remaining": _split_remaining, "is_complete": _split_is_complete, "payment_id": _split_first_payment_id} if split_mode else {}),
                    }
                }

        # Award waros AFTER the transaction commits — avoids race condition where
        # create_task runs at an await point inside the transaction before commit.
        if _order_status == 'completed' and _customer_id:
            try:
                asyncio.create_task(
                    evaluate_and_award(_order_id, _customer_id, _tenant_id)
                )
            except Exception as _waros_err:
                logger.warning(f"Could not schedule waros evaluation: {_waros_err}")

        # Send receipt email if requested — fire-and-forget, never blocks or fails the order
        if _order_status == 'completed' and receipt_email:
            try:
                # Look up tenant public profile for business branding in the receipt
                _business_name = None
                _business_address = None
                _business_city = None
                _business_phone = None
                try:
                    async with get_db_connection() as _profile_conn:
                        _profile_row = await _profile_conn.fetchrow(
                            """
                            SELECT
                                COALESCE(p.display_name, t.name) AS display_name,
                                p.address,
                                p.city,
                                p.phone_number
                            FROM tenants t
                            LEFT JOIN tenant_public_profiles p ON p.tenant_id = t.id
                            WHERE t.id = $1
                            """,
                            _tenant_id
                        )
                        if _profile_row:
                            _business_name = _profile_row['display_name']
                            _business_address = _profile_row['address']
                            _business_city = _profile_row['city']
                            _business_phone = _profile_row['phone_number']
                except Exception as _profile_err:
                    logger.warning(f"Could not fetch tenant profile for receipt: {_profile_err}")

                _waro_disc = float(_waro_redemption_summary.get("waro_discount_cop") or 0)
                _email_subtotal = (
                    float(cart_subtotal)
                    if (_discount_amount or _promo_savings or _waro_disc)
                    else 0.0
                )
                asyncio.create_task(
                    _send_tracked_pos_receipt_email(
                        customer_email=receipt_email,
                        order_id=_order_id,
                        tenant_id=_tenant_id,
                        order_number=_order_number,
                        total_amount=_total_amount,
                        payment_method=payment_method,
                        items=_items,
                        order_date=_order_date,
                        business_name=_business_name,
                        business_address=_business_address,
                        business_city=_business_city,
                        business_phone=_business_phone,
                        discount_amount=float(_discount_amount) if _discount_amount else 0.0,
                        subtotal=_email_subtotal,
                        promo_savings=float(_promo_savings),
                        promo_breakdown=_promo_breakdown,
                        waro_redemption_summary=_waro_redemption_summary,
                        tip_amount=float(tip_amount),
                    )
                )
            except Exception as _email_err:
                logger.warning(f"Could not schedule receipt email: {_email_err}")

        return _result

    except (AuthenticationError, APIError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error completing order: {str(e)}")
        raise APIError(f"Error completing order: {str(e)}", status_code=500)


# ── order_item_ingredients helpers ────────────────────────────────────────────

async def _get_last_purchase_prices(conn, ingredient_ids: List[str], tenant_id: str) -> dict:
    """
    Fetch the most recent unit_cost from tenant_purchase_items for each ingredient_id.
    Returns a dict { ingredient_id_str: unit_cost_float }.
    Missing ingredients get None.
    """
    if not ingredient_ids:
        return {}
    rows = await conn.fetch(
        """
        SELECT DISTINCT ON (tpi.ingredient_id)
            tpi.ingredient_id::text,
            tpi.unit_cost
        FROM tenant_purchase_items tpi
        JOIN tenant_purchases tp ON tp.id = tpi.purchase_id AND tp.tenant_id = $1
        WHERE tpi.ingredient_id = ANY($2::uuid[])
          AND tpi.unit_cost IS NOT NULL
        ORDER BY tpi.ingredient_id, tp.created_at DESC
        """,
        tenant_id,
        ingredient_ids,
    )
    return {r["ingredient_id"]: float(r["unit_cost"]) for r in rows}

# Aggregate qty/cost when product recipe and modifier share an ingredient; replace on
# same-source retry so re-running capture for one source does not double-count.
_ORDER_ITEM_INGREDIENT_UPSERT = """
ON CONFLICT (order_item_id, ingredient_id) DO UPDATE SET
  quantity = CASE
    WHEN order_item_ingredients.source_type IS NOT DISTINCT FROM EXCLUDED.source_type
     AND order_item_ingredients.source_id IS NOT DISTINCT FROM EXCLUDED.source_id
    THEN EXCLUDED.quantity
    ELSE order_item_ingredients.quantity + EXCLUDED.quantity
  END,
  unit_cost = COALESCE(order_item_ingredients.unit_cost, EXCLUDED.unit_cost),
  total_cost = CASE
    WHEN COALESCE(EXCLUDED.unit_cost, order_item_ingredients.unit_cost) IS NOT NULL THEN
      (CASE
        WHEN order_item_ingredients.source_type IS NOT DISTINCT FROM EXCLUDED.source_type
         AND order_item_ingredients.source_id IS NOT DISTINCT FROM EXCLUDED.source_id
        THEN EXCLUDED.quantity
        ELSE order_item_ingredients.quantity + EXCLUDED.quantity
      END) * COALESCE(EXCLUDED.unit_cost, order_item_ingredients.unit_cost)
    ELSE NULL
  END
"""


async def _capture_order_item_ingredients(
    conn,
    order_item_id,
    product_id,
    item_quantity: float,
    tenant_id: str,
) -> None:
    """
    Insert ingredient snapshots into order_item_ingredients for a given order_item.

    Sources:
    - product_recipes (source_type = 'product_recipe')
    - product_base_recipes → base_recipe_templates (source_type = 'base_recipe')

    Idempotent per source on conflict; aggregates qty/cost across product + modifier.
    """
    rows = await conn.fetch(
        """
        SELECT
            pr.id::text        AS source_id,
            'PRODUCT_RECIPE'   AS source_type,
            pr.ingredient_id,
            i.name             AS ingredient_name,
            pr.quantity,
            pr.unit
        FROM product_recipes pr
        JOIN ingredients i ON i.id = pr.ingredient_id
        WHERE pr.product_id = $1

        UNION ALL

        SELECT
            brt.id::text                              AS source_id,
            'PRODUCT_RECIPE'                          AS source_type,
            brt.ingredient_id,
            i.name                                    AS ingredient_name,
            brt.base_quantity * pbr.quantity          AS quantity,  -- Issue #517 multiplier
            brt.unit
        FROM product_base_recipes pbr
        JOIN base_recipe_templates brt ON brt.product_base_type_id = pbr.product_base_type_id
        JOIN ingredients i ON i.id = brt.ingredient_id
        WHERE pbr.product_id = $1
        """,
        product_id,
    )

    if not rows:
        return

    ingredient_ids = [str(r["ingredient_id"]) for r in rows]
    prices = await _get_last_purchase_prices(conn, ingredient_ids, tenant_id)

    for r in rows:
        ingredient_id_str = str(r["ingredient_id"])
        resolved_qty = await resolve_recipe_quantity_to_base_unit(
            conn,
            r["ingredient_id"],
            float(r["quantity"]),
            r["unit"] or "",
        )
        quantity = resolved_qty * item_quantity
        unit_cost = prices.get(ingredient_id_str)
        total_cost = quantity * unit_cost if unit_cost is not None else None

        await conn.execute(
            """
            INSERT INTO order_item_ingredients (
                order_item_id, ingredient_id, ingredient_name,
                quantity, unit, unit_cost, total_cost,
                source_type, source_id, created_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::uuid, NOW())
            """
            + _ORDER_ITEM_INGREDIENT_UPSERT,
            order_item_id,
            r["ingredient_id"],
            r["ingredient_name"],
            quantity,
            r["unit"] or "und",
            unit_cost,
            total_cost,
            r["source_type"],
            r["source_id"],
        )


async def _deduct_modifier_inventory_for_order_item(
    conn,
    *,
    tenant_id,
    user_id,
    order_id,
    order_item_id,
    order_number,
    item_quantity: float,
    modifier: dict,
    modifier_qty: float = 1,
) -> None:
    """Deduct inventory for one order line modifier (all option types). Issue #1121."""
    from app.services.modifier_option_service import resolve_modifier_ingredient_lines

    modifier_id = modifier.get("id")
    modifier_name = modifier.get("name", "Modificador")
    if not modifier_id:
        return

    ingredient_lines = await resolve_modifier_ingredient_lines(
        conn, modifier_id, tenant_id
    )
    if not ingredient_lines:
        return

    for line in ingredient_lines:
        total_deduction = (
            float(item_quantity) * float(modifier_qty) * float(line["quantity"])
        )

        if line.get("controla_inventario") is not False:
            stock_row = await conn.fetchrow(
                """
                SELECT current_stock FROM tenant_inventory
                WHERE ingredient_id = $1 AND tenant_id = $2
                """,
                line["ingredient_id"],
                tenant_id,
            )

            previous_stock = float(stock_row["current_stock"]) if stock_row else 0.0
            new_stock = previous_stock - total_deduction

            if stock_row:
                await conn.execute(
                    """
                    UPDATE tenant_inventory
                    SET current_stock = $1, last_updated = NOW()
                    WHERE ingredient_id = $2 AND tenant_id = $3
                    """,
                    new_stock,
                    line["ingredient_id"],
                    tenant_id,
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO tenant_inventory (
                        tenant_id, ingredient_id, current_stock, minimum_stock, last_updated
                    )
                    VALUES ($1, $2, $3, 0, NOW())
                    """,
                    tenant_id,
                    line["ingredient_id"],
                    -total_deduction,
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
                line["ingredient_id"],
                "consumption",
                -total_deduction,
                line["unit"] or "und",
                previous_stock,
                new_stock,
                "orders",
                order_id,
                f"Modificador {modifier_name} ({modifier_qty}x) - Orden #{order_number}",
                user_id,
            )

            logger.info(
                f"Modifier inventory deducted: {line['ingredient_name']} "
                f"-{total_deduction}{line['unit']} "
                f"(Modifier: {modifier_name}, Order #{order_number})"
            )

        await _capture_modifier_ingredient_line_snapshot(
            conn,
            order_item_id,
            line,
            modifier_id,
            item_quantity,
            float(modifier_qty),
            str(tenant_id),
        )


async def _capture_modifier_ingredient_line_snapshot(
    conn,
    order_item_id,
    line: dict,
    modifier_id,
    item_quantity: float,
    modifier_qty: float,
    tenant_id: str,
) -> None:
    """
    COGS snapshot for one exploded modifier ingredient line.
    source_type = 'MODIFIER_RECIPE', source_id = modifier_id.
    """
    ingredient_id = line["ingredient_id"]
    ingredient_id_str = str(ingredient_id)
    prices = await _get_last_purchase_prices(conn, [ingredient_id_str], tenant_id)

    quantity = float(line["quantity"]) * item_quantity * modifier_qty
    unit_cost = prices.get(ingredient_id_str)
    total_cost = quantity * unit_cost if unit_cost is not None else None

    await conn.execute(
        """
        INSERT INTO order_item_ingredients (
            order_item_id, ingredient_id, ingredient_name,
            quantity, unit, unit_cost, total_cost,
            source_type, source_id, created_at
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::uuid, NOW())
        """
        + _ORDER_ITEM_INGREDIENT_UPSERT,
        order_item_id,
        ingredient_id,
        line["ingredient_name"],
        quantity,
        line.get("unit") or "und",
        unit_cost,
        total_cost,
        "MODIFIER_RECIPE",
        str(modifier_id),
    )


async def _capture_modifier_ingredient_snapshot(
    conn,
    order_item_id,
    modifier_ingredient,
    modifier_id,
    item_quantity: float,
    modifier_qty: float,
    tenant_id: str,
) -> None:
    """Backward-compatible wrapper for single-ingredient modifier rows (tests)."""
    ing_qty = modifier_ingredient.get("ingredient_quantity")
    if not ing_qty:
        return

    resolved_ing_qty = await resolve_recipe_quantity_to_base_unit(
        conn,
        modifier_ingredient["ingredient_id"],
        float(ing_qty),
        modifier_ingredient.get("ingredient_unit") or "",
    )
    await _capture_modifier_ingredient_line_snapshot(
        conn,
        order_item_id,
        {
            "ingredient_id": modifier_ingredient["ingredient_id"],
            "quantity": resolved_ing_qty,
            "unit": modifier_ingredient.get("ingredient_unit") or "und",
            "ingredient_name": modifier_ingredient["ingredient_name"],
        },
        modifier_id,
        item_quantity,
        modifier_qty,
        tenant_id,
    )


async def _subtract_order_item_ingredient_snapshot(
    conn,
    order_item_id,
    ingredient_id,
    quantity: float,
) -> None:
    """Reduce or remove a COGS snapshot row after returning modifier stock."""
    row = await conn.fetchrow(
        """
        SELECT quantity, unit_cost
        FROM order_item_ingredients
        WHERE order_item_id = $1 AND ingredient_id = $2
        """,
        order_item_id,
        ingredient_id,
    )
    if not row:
        return

    new_qty = float(row["quantity"] or 0) - quantity
    if new_qty <= 0:
        await conn.execute(
            """
            DELETE FROM order_item_ingredients
            WHERE order_item_id = $1 AND ingredient_id = $2
            """,
            order_item_id,
            ingredient_id,
        )
        return

    unit_cost = row["unit_cost"]
    total_cost = new_qty * float(unit_cost) if unit_cost is not None else None
    await conn.execute(
        """
        UPDATE order_item_ingredients
        SET quantity = $3,
            total_cost = $4
        WHERE order_item_id = $1 AND ingredient_id = $2
        """,
        order_item_id,
        ingredient_id,
        new_qty,
        total_cost,
    )


async def return_order_item_inventory_from_snapshots(
    conn,
    *,
    tenant_id,
    user_id,
    order_id,
    order_number: int,
    order_item_id,
    reason_detail: str,
) -> bool:
    """
    Return stock from order_item_ingredients and delete snapshot rows.
    Returns True when snapshots existed (caller may skip legacy recipe returns).
    """
    from app.services.orders_service import _return_ingredient_to_stock

    snapshots = await conn.fetch(
        """
        SELECT ingredient_id, ingredient_name, quantity, unit
        FROM order_item_ingredients
        WHERE order_item_id = $1
        """,
        order_item_id,
    )
    if not snapshots:
        return False

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
            reason_detail,
        )

    await conn.execute(
        "DELETE FROM order_item_ingredients WHERE order_item_id = $1",
        order_item_id,
    )
    return True


async def return_modifier_inventory_for_order_item(
    conn,
    *,
    tenant_id,
    user_id,
    order_id,
    order_number: int,
    order_item_id,
    item_quantity: float,
    modifier_id,
    modifier_qty: float,
    modifier_name: str,
    product_name: str,
) -> None:
    """Reverse composite modifier consumption and adjust COGS snapshots."""
    from app.services.modifier_option_service import resolve_modifier_ingredient_lines
    from app.services.orders_service import _return_ingredient_to_stock

    ingredient_lines = await resolve_modifier_ingredient_lines(
        conn, modifier_id, tenant_id
    )
    if not ingredient_lines:
        return

    for line in ingredient_lines:
        qty = float(item_quantity) * float(modifier_qty) * float(line["quantity"])
        if qty <= 0:
            continue

        if line.get("controla_inventario") is not False:
            await _return_ingredient_to_stock(
                conn,
                tenant_id,
                user_id,
                order_id,
                order_number,
                line["ingredient_id"],
                qty,
                line.get("unit") or "und",
                line["ingredient_name"],
                f"Devolución modificador {modifier_name} de {product_name}",
            )

        await _subtract_order_item_ingredient_snapshot(
            conn,
            order_item_id,
            line["ingredient_id"],
            qty,
        )


async def fire_pos_cart(request: Request, cart_id: UUID) -> dict:
    """
    Explicitly fire all 'new' items in a POS cart to the kitchen stations.
    This is used when a restaurant wants to 'fire' the order before payment.
    Since POS carts don't have orders yet until 'complete', we cannot fire them
    directly using fire_comandas (which requires an order_id).
    
    HOWEVER, for consistency with the issue spec which asks for cart fire,
    we have two options:
    1. Create a dummy 'provisional' order to fire.
    2. Change fire_comandas to work with carts (too complex).
    3. Industry standard: most POS systems (except table ones) send to kitchen
       at the moment of payment (auto-fire).
    
    GIVEN the issue requirement: "Endpoint 2 — Counter/POS fire ... same as mesa fire
    but scoped to a pos_cart's order." This implies the order MUST exist.
    
    If no order exists for the cart, we return error.
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

            # 2. Find the most recent order linked to this cart
            order_row = await conn.fetchrow(
                "SELECT id, order_number, delivery_address_id "
                "FROM orders WHERE pos_cart_id = $1 AND tenant_id = $2 "
                "ORDER BY created_at DESC LIMIT 1",
                cart_id, tenant_id
            )
            if not order_row:
                raise APIError("No se encontró una orden asociada a este carrito. Complete la orden primero.", status_code=400)

            # 3. Fire
            _is_delivery = order_row["delivery_address_id"] is not None
            async with conn.transaction():
                comandas = await fire_comandas(
                    order_id=order_row["id"],
                    tenant_id=tenant_id,
                    source_type='delivery' if _is_delivery else 'pos',
                    table_display_name=(
                        f'Domicilio #{order_row["order_number"]}' if _is_delivery else 'Mostrador'
                    ),
                    conn=conn
                )

        total_fired = sum(len(c.get('items', [])) for c in comandas)
        return {
            "success": True,
            "data": {
                "comandas": comandas,
                "fired_items_count": total_fired
            }
        }

    except (AuthenticationError, APIError):
        raise
    except Exception as e:
        logger.error(f"Error firing POS cart {cart_id}: {e}")
        raise APIError(f"Error firing POS cart: {e}", status_code=500)

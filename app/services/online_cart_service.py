"""
Online Cart Service
Handles cart operations for public online ordering (NO authentication required)
"""
from typing import List, Optional
from uuid import UUID
from fastapi import HTTPException
from app.database import get_db_connection
from app.core.exceptions import APIError
from app.services.email_helpers import send_order_confirmation_email
import logging
import datetime as _dt
from decimal import Decimal

logger = logging.getLogger(__name__)


async def get_cart_items(conn, cart_id: UUID) -> List[dict]:
    """Get all items in cart with their modifiers"""
    items_query = """
        SELECT
            oci.id,
            oci.product_id,
            p.name as product_name,
            oci.quantity,
            oci.unit_price,
            oci.subtotal,
            oci.notes
        FROM online_cart_items oci
        JOIN product p ON oci.product_id = p.id
        WHERE oci.cart_id = $1
        ORDER BY oci.created_at
    """
    item_rows = await conn.fetch(items_query, cart_id)

    items = []
    for item_row in item_rows:
        # Get modifiers for this item
        modifiers_query = """
            SELECT id, modifier_id, modifier_name, price
            FROM online_cart_item_modifiers
            WHERE cart_item_id = $1
        """
        modifier_rows = await conn.fetch(modifiers_query, item_row['id'])

        items.append({
            "id": str(item_row['id']),
            "product_id": str(item_row['product_id']),
            "product_name": item_row['product_name'],
            "quantity": item_row['quantity'],
            "unit_price": float(item_row['unit_price']),
            "subtotal": float(item_row['subtotal']),
            "notes": item_row['notes'],
            "modifiers": [
                {
                    "id": str(mod['id']),
                    "modifier_id": str(mod['modifier_id']),
                    "modifier_name": mod['modifier_name'],
                    "price": float(mod['price'])
                }
                for mod in modifier_rows
            ]
        })

    return items


def calculate_item_subtotal(unit_price: float, modifiers: List[dict], quantity: int) -> float:
    """Calculate item subtotal including modifiers"""
    modifier_total = sum(mod.get('price', 0) for mod in modifiers)
    return (unit_price + modifier_total) * quantity


async def validate_modifiers_for_item(conn, product_id: UUID, modifiers: List[dict]) -> None:
    """
    Validate modifiers submitted for a cart item.

    Checks:
    1. All modifier IDs exist in the modifiers table and are active (is_available=true)
    2. Required modifier groups (is_required=true) have at least one modifier selected
    3. No group exceeds its max_qty limit

    Raises HTTPException(422) with a descriptive message on any violation.
    """
    requested_ids = [UUID(str(mod['id'])) for mod in modifiers]

    # 1. Batch-validate existence and availability in one query
    valid_rows = await conn.fetch(
        """
        SELECT id, modifier_group_id
        FROM modifiers
        WHERE id = ANY($1::uuid[]) AND is_available = true
        """,
        requested_ids
    )
    valid_id_set = {row['id'] for row in valid_rows}
    for mod_id in requested_ids:
        if mod_id not in valid_id_set:
            raise HTTPException(
                status_code=422,
                detail=f"Modifier '{mod_id}' does not exist or is not available"
            )

    # Build map: modifier_id → group_id (from validated rows)
    modifier_to_group = {row['id']: row['modifier_group_id'] for row in valid_rows}

    # 2 & 3. Load the product's modifier groups via junction table
    group_rows = await conn.fetch(
        """
        SELECT mg.id, mg.name, mg.is_required, mg.min_qty, mg.max_qty
        FROM product_modifier_groups pmg
        JOIN modifier_groups mg ON mg.id = pmg.modifier_group_id
        WHERE pmg.product_id = $1
        """,
        product_id
    )

    if not group_rows:
        # No modifier groups defined for this product — no group-level rules to enforce
        return

    # Count how many submitted modifiers belong to each group
    group_selection_count: dict = {}
    for mod_id in requested_ids:
        group_id = modifier_to_group.get(mod_id)
        if group_id:
            group_selection_count[group_id] = group_selection_count.get(group_id, 0) + 1

    for group in group_rows:
        group_id = group['id']
        count = group_selection_count.get(group_id, 0)
        min_required = max(1, group['min_qty']) if group['is_required'] else group['min_qty']

        if group['is_required'] and count < min_required:
            raise HTTPException(
                status_code=422,
                detail=f"Group '{group['name']}' requires at least {min_required} selection(s), got {count}"
            )
        if count > group['max_qty']:
            raise HTTPException(
                status_code=422,
                detail=f"Group '{group['name']}' allows at most {group['max_qty']} selection(s), got {count}"
            )


async def validate_products_belong_to_tenant(conn, product_ids: List[UUID], tenant_id: UUID) -> None:
    """
    Ensure all product_ids belong to the given tenant.

    Uses a single bulk query against the product.tenant_id index.
    Raises HTTPException(422) if any product is from a different tenant or has no tenant set.
    """
    if not product_ids:
        return
    rows = await conn.fetch(
        """
        SELECT id FROM product
        WHERE id = ANY($1::uuid[])
          AND (tenant_id != $2 OR tenant_id IS NULL)
        """,
        product_ids,
        tenant_id
    )
    if rows:
        raise HTTPException(
            status_code=422,
            detail="One or more products do not belong to this restaurant."
        )


async def create_cart_with_batch_items(
    tenant_id: UUID,
    items: List[dict],
    session_id: Optional[str] = None,
    order_type: str = 'delivery'
) -> dict:
    """
    Create online cart and add all items in batch (PUBLIC - no auth required).
    Session-based for anonymous users.
    """
    try:
        async with get_db_connection() as conn:
            async with conn.transaction():
                # Create cart
                create_cart_query = """
                    INSERT INTO online_carts (
                        tenant_id, session_id, order_type, status, total_amount
                    )
                    VALUES ($1, $2, $3, 'active', 0)
                    RETURNING id, total_amount, created_at, updated_at
                """
                cart_row = await conn.fetchrow(
                    create_cart_query,
                    tenant_id,
                    session_id,
                    order_type
                )
                cart_id = cart_row['id']
                logger.info(f"Created online cart: {cart_id}")

                # Validate all products belong to this tenant before inserting
                product_ids = [UUID(str(item['product_id'])) for item in items]
                await validate_products_belong_to_tenant(conn, product_ids, tenant_id)

                # Add all items
                cart_total = Decimal('0')
                for item_data in items:
                    product_id = item_data['product_id']
                    quantity = item_data['quantity']
                    unit_price = Decimal(str(item_data['unit_price']))
                    modifiers = item_data.get('modifiers', [])
                    notes = item_data.get('notes')

                    # Validate modifiers before insert
                    if modifiers:
                        await validate_modifiers_for_item(conn, UUID(str(product_id)), modifiers)

                    # Calculate subtotal
                    subtotal = calculate_item_subtotal(
                        float(unit_price),
                        modifiers,
                        quantity
                    )

                    # Insert cart item
                    item_insert_query = """
                        INSERT INTO online_cart_items (
                            cart_id, product_id, quantity, unit_price, subtotal, notes
                        )
                        VALUES ($1, $2, $3, $4, $5, $6)
                        RETURNING id
                    """
                    item_row = await conn.fetchrow(
                        item_insert_query,
                        cart_id,
                        product_id,
                        quantity,
                        unit_price,
                        Decimal(str(subtotal)),
                        notes
                    )
                    item_id = item_row['id']

                    # Insert modifiers
                    for mod in modifiers:
                        mod_insert_query = """
                            INSERT INTO online_cart_item_modifiers (
                                cart_item_id, modifier_id, modifier_name, price
                            )
                            VALUES ($1, $2, $3, $4)
                        """
                        await conn.execute(
                            mod_insert_query,
                            item_id,
                            mod['id'],
                            mod['name'],
                            Decimal(str(mod['price']))
                        )

                    cart_total += Decimal(str(subtotal))

                # Update cart total
                await conn.execute(
                    "UPDATE online_carts SET total_amount = $1, updated_at = now() WHERE id = $2",
                    cart_total,
                    cart_id
                )

                # Get complete cart with items
                items_list = await get_cart_items(conn, cart_id)

                return {
                    "success": True,
                    "data": {
                        "id": str(cart_id),
                        "total_amount": float(cart_total),
                        "items": items_list,
                        "session_id": session_id,
                        "order_type": order_type,
                        "created_at": cart_row['created_at'].isoformat(),
                        "updated_at": cart_row['updated_at'].isoformat()
                    }
                }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating online cart with batch: {str(e)}")
        raise APIError(f"Error creating cart: {str(e)}", status_code=500)


async def get_cart_by_session(session_id: str, tenant_id: UUID) -> dict:
    """Get active cart by session ID (PUBLIC)"""
    try:
        async with get_db_connection() as conn:
            cart_query = """
                SELECT id, tenant_id, customer_id, session_id, order_type,
                       delivery_address_id, scheduled_time, delivery_instructions,
                       pickup_pin, is_verified, verified_email, status, total_amount,
                       created_at, updated_at
                FROM online_carts
                WHERE session_id = $1
                AND tenant_id = $2
                AND status = 'active'
                ORDER BY updated_at DESC
                LIMIT 1
            """
            cart_row = await conn.fetchrow(cart_query, session_id, tenant_id)

            if not cart_row:
                raise HTTPException(status_code=404, detail="Cart not found")

            cart_id = cart_row['id']
            items = await get_cart_items(conn, cart_id)

            return {
                "success": True,
                "data": {
                    "id": str(cart_id),
                    "tenant_id": str(cart_row['tenant_id']),
                    "customer_id": str(cart_row['customer_id']) if cart_row['customer_id'] else None,
                    "session_id": cart_row['session_id'],
                    "order_type": cart_row['order_type'],
                    "delivery_address_id": str(cart_row['delivery_address_id']) if cart_row['delivery_address_id'] else None,
                    "scheduled_time": cart_row['scheduled_time'].isoformat() if cart_row['scheduled_time'] else None,
                    "delivery_instructions": cart_row['delivery_instructions'],
                    "pickup_pin": cart_row['pickup_pin'],
                    "is_verified": cart_row['is_verified'],
                    "verified_email": cart_row['verified_email'],
                    "status": cart_row['status'],
                    "total_amount": float(cart_row['total_amount']),
                    "items": items,
                    "created_at": cart_row['created_at'].isoformat(),
                    "updated_at": cart_row['updated_at'].isoformat()
                }
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting cart by session: {str(e)}")
        raise APIError(f"Error getting cart: {str(e)}", status_code=500)


async def update_delivery_info(
    cart_id: UUID,
    order_type: str,
    delivery_address_id: Optional[UUID] = None,
    scheduled_time: Optional[_dt.datetime] = None,
    delivery_instructions: Optional[str] = None
) -> dict:
    """Update delivery information for cart (PUBLIC)"""
    try:
        async with get_db_connection() as conn:
            update_query = """
                UPDATE online_carts
                SET order_type = $1,
                    delivery_address_id = $2,
                    scheduled_time = $3,
                    delivery_instructions = $4,
                    updated_at = now()
                WHERE id = $5
                AND status = 'active'
                RETURNING id
            """
            result = await conn.fetchrow(
                update_query,
                order_type,
                delivery_address_id,
                scheduled_time,
                delivery_instructions,
                cart_id
            )

            if not result:
                raise HTTPException(status_code=404, detail="Cart not found or not active")

            return {
                "success": True,
                "message": "Delivery info updated successfully"
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating delivery info: {str(e)}")
        raise APIError(f"Error updating delivery info: {str(e)}", status_code=500)


async def delete_cart_item(cart_id: UUID, item_id: UUID) -> dict:
    """Delete item from cart and recalculate total (PUBLIC)"""
    try:
        async with get_db_connection() as conn:
            async with conn.transaction():
                # Delete item (CASCADE will delete modifiers)
                delete_query = """
                    DELETE FROM online_cart_items
                    WHERE id = $1 AND cart_id = $2
                    RETURNING subtotal
                """
                deleted_row = await conn.fetchrow(delete_query, item_id, cart_id)

                if not deleted_row:
                    raise HTTPException(status_code=404, detail="Item not found")

                # Recalculate cart total
                total_query = """
                    SELECT COALESCE(SUM(subtotal), 0) as total
                    FROM online_cart_items
                    WHERE cart_id = $1
                """
                total_row = await conn.fetchrow(total_query, cart_id)
                new_total = total_row['total']

                # Update cart
                await conn.execute(
                    "UPDATE online_carts SET total_amount = $1, updated_at = now() WHERE id = $2",
                    new_total,
                    cart_id
                )

                return {
                    "success": True,
                    "message": "Item deleted successfully",
                    "new_total": float(new_total)
                }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting cart item: {str(e)}")
        raise APIError(f"Error deleting item: {str(e)}", status_code=500)


async def clear_cart(cart_id: UUID) -> dict:
    """Clear all items from cart (PUBLIC)"""
    try:
        async with get_db_connection() as conn:
            async with conn.transaction():
                # Delete all items
                await conn.execute(
                    "DELETE FROM online_cart_items WHERE cart_id = $1",
                    cart_id
                )

                # Reset cart total
                await conn.execute(
                    "UPDATE online_carts SET total_amount = 0, updated_at = now() WHERE id = $1",
                    cart_id
                )

                return {
                    "success": True,
                    "message": "Cart cleared successfully"
                }

    except Exception as e:
        logger.error(f"Error clearing cart: {str(e)}")
        raise APIError(f"Error clearing cart: {str(e)}", status_code=500)


async def associate_customer_to_cart(cart_id: UUID, customer_id: UUID) -> dict:
    """Associate customer to cart after verification (PUBLIC)"""
    try:
        async with get_db_connection() as conn:
            update_query = """
                UPDATE online_carts
                SET customer_id = $1, updated_at = now()
                WHERE id = $2
                AND status = 'active'
                RETURNING id
            """
            result = await conn.fetchrow(update_query, customer_id, cart_id)

            if not result:
                raise HTTPException(status_code=404, detail="Cart not found or not active")

            return {
                "success": True,
                "message": "Customer associated successfully"
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error associating customer: {str(e)}")
        raise APIError(f"Error associating customer: {str(e)}", status_code=500)


async def verify_cart_with_session(cart_id: UUID, customer_id: UUID, email: str) -> dict:
    """
    Link a cart to an already-authenticated customer session (PUBLIC endpoint, JWT-gated).

    Called when the customer has a valid waro_customer_session cookie so the OTP
    flow was skipped. Sets is_verified = true and customer_id on the cart, and
    generates a pickup_pin for pickup orders (mirrors verify_otp_code logic).
    """
    try:
        async with get_db_connection() as conn:
            async with conn.transaction():
                cart_query = """
                    SELECT id, order_type, customer_id, pickup_pin
                    FROM online_carts
                    WHERE id = $1 AND status = 'active'
                """
                cart = await conn.fetchrow(cart_query, cart_id)

                if not cart:
                    raise HTTPException(status_code=404, detail="Carrito no encontrado o ya procesado")

                # Guard: if already linked to a different customer, refuse
                if cart['customer_id'] and cart['customer_id'] != customer_id:
                    raise HTTPException(
                        status_code=409,
                        detail="El carrito ya está asociado a otro cliente"
                    )

                # Update cart with verified session data
                await conn.execute(
                    """
                    UPDATE online_carts
                    SET is_verified = true,
                        verified_email = $1,
                        customer_id = $2,
                        updated_at = now()
                    WHERE id = $3
                    """,
                    email,
                    customer_id,
                    cart_id,
                )

                # Generate pickup_pin for pickup orders if not already set
                pickup_pin = cart['pickup_pin']
                if cart['order_type'] == 'pickup' and not pickup_pin:
                    from app.services.otp_service import generate_pickup_pin
                    pickup_pin = generate_pickup_pin()
                    await conn.execute(
                        "UPDATE online_carts SET pickup_pin = $1, pin_generated_at = now() WHERE id = $2",
                        pickup_pin,
                        cart_id,
                    )

                logger.info(f"Cart {cart_id} verified via session for customer {customer_id}")

                return {
                    "success": True,
                    "customer_id": str(customer_id),
                    "email": email,
                    "is_verified": True,
                    "pickup_pin": pickup_pin,
                }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error verifying cart with session for cart {cart_id}: {str(e)}")
        raise APIError(f"Error al verificar el carrito: {str(e)}", status_code=500)


async def checkout_cart(cart_id: UUID) -> dict:
    """
    Convert a verified online cart into a confirmed order (PUBLIC).

    Validates cart state, atomically marks it as checked_out, then inserts
    the order, order_items, and order_item_modifiers in a single transaction.

    Returns order summary with order_number and optional pickup_pin.
    Returns 409 if the cart was already checked out (double-submit prevention).
    """
    try:
        async with get_db_connection() as conn:
            async with conn.transaction():
                # 1. Fetch cart
                cart_query = """
                    SELECT id, tenant_id, customer_id, order_type,
                           delivery_address_id, pickup_pin, is_verified, status, total_amount,
                           verified_email, scheduled_time, delivery_instructions
                    FROM online_carts
                    WHERE id = $1
                """
                cart = await conn.fetchrow(cart_query, cart_id)

                if not cart:
                    raise HTTPException(status_code=404, detail="Carrito no encontrado")

                # 2. Validate state
                if not cart['is_verified']:
                    raise HTTPException(status_code=400, detail="El carrito no ha sido verificado. Completa el proceso de verificación por OTP.")

                if cart['status'] == 'checked_out':
                    raise HTTPException(status_code=409, detail="Este carrito ya fue procesado. El pedido ya existe.")

                if cart['status'] != 'active':
                    raise HTTPException(status_code=400, detail=f"El carrito no está activo (estado: {cart['status']})")

                if cart['order_type'] == 'delivery' and not cart['delivery_address_id']:
                    raise HTTPException(status_code=400, detail="Se requiere una dirección de entrega para pedidos a domicilio.")

                # 3. Fetch items (reuse existing helper)
                items = await get_cart_items(conn, cart_id)

                if not items:
                    raise HTTPException(status_code=400, detail="El carrito está vacío")

                # 4. Validate open status and minimum order amount from tenant profile
                tenant_profile_query = """
                    SELECT min_order_amount, estimated_preparation_time, is_manually_open, business_hours
                    FROM tenant_public_profiles
                    WHERE tenant_id = $1
                """
                profile = await conn.fetchrow(tenant_profile_query, cart['tenant_id'])
                min_order_amount = Decimal('0')
                estimated_preparation_time = None
                if profile:
                    from app.services.public_restaurant_service import is_currently_open
                    import json as _json
                    bh = profile['business_hours']
                    if isinstance(bh, str):
                        bh = _json.loads(bh)
                    if not is_currently_open(bh, profile['is_manually_open']):
                        raise HTTPException(
                            status_code=409,
                            detail="El restaurante está cerrado en este momento. No se pueden procesar pedidos."
                        )
                    min_order_amount = Decimal(str(profile['min_order_amount'] or '0'))
                    estimated_preparation_time = profile['estimated_preparation_time']

                cart_total = Decimal(str(cart['total_amount'] or '0'))
                if cart_total < min_order_amount:
                    raise HTTPException(
                        status_code=400,
                        detail=f"El monto mínimo del pedido es ${min_order_amount:,.0f} COP. Tu carrito tiene ${cart_total:,.0f} COP."
                    )

                # 5. Atomically lock cart — prevents double checkout
                locked = await conn.fetchrow(
                    "UPDATE online_carts SET status = 'checked_out', completed_at = now(), updated_at = now() WHERE id = $1 AND status = 'active' RETURNING id",
                    cart_id
                )
                if not locked:
                    raise HTTPException(status_code=409, detail="El pedido ya fue procesado por otra solicitud simultánea.")

                # 6. Create order
                order_query = """
                    INSERT INTO orders (
                        tenant_id, customer_id, online_cart_id,
                        order_date, total_amount, status, scheduled_time
                    )
                    VALUES ($1, $2, $3, NOW(), $4, 'pending', $5)
                    RETURNING id, order_number
                """
                order_row = await conn.fetchrow(
                    order_query,
                    cart['tenant_id'],
                    cart['customer_id'],
                    cart_id,
                    cart_total,
                    cart['scheduled_time']
                )
                order_id = order_row['id']
                order_number = order_row['order_number']

                logger.info(f"Created online order #{order_number} from cart {cart_id}")

                # 7. Copy items to order_items
                for item in items:
                    order_item_query = """
                        INSERT INTO order_items (
                            order_id, product_id, quantity, price_at_purchase, subtotal
                        )
                        VALUES ($1, $2, $3, $4, $5)
                        RETURNING id
                    """
                    order_item_row = await conn.fetchrow(
                        order_item_query,
                        order_id,
                        item['product_id'],
                        item['quantity'],
                        Decimal(str(item['unit_price'])),
                        Decimal(str(item['subtotal']))
                    )
                    order_item_id = order_item_row['id']

                    # 8. Copy modifiers to order_item_modifiers
                    for mod in item['modifiers']:
                        await conn.execute(
                            """
                            INSERT INTO order_item_modifiers (
                                order_item_id, modifier_id, modifier_name, price_at_purchase, quantity
                            )
                            VALUES ($1, $2, $3, $4, $5)
                            """,
                            order_item_id,
                            mod['modifier_id'],
                            mod['modifier_name'],
                            Decimal(str(mod['price'])),
                            1
                        )

                # 9. Fetch delivery address for the confirmation email (inside transaction,
                #    but read-only — does not affect atomicity)
                delivery_address = None
                if cart['order_type'] == 'delivery' and cart['delivery_address_id']:
                    addr_row = await conn.fetchrow(
                        """
                        SELECT address_line1, address_line2, city, state, delivery_notes
                        FROM addresses_profile
                        WHERE id = $1
                        """,
                        cart['delivery_address_id'],
                    )
                    if addr_row:
                        delivery_address = dict(addr_row)

                # 10. Notify operators of new order (best-effort — never fail the checkout)
                try:
                    from app.services import notifications_service as _notif_svc  # local import avoids circular dep
                    customer_name = None
                    if cart.get('customer_id'):
                        name_row = await conn.fetchrow(
                            "SELECT name FROM profile WHERE id = $1",
                            cart['customer_id'],
                        )
                        if name_row:
                            customer_name = name_row['name'] or None

                    await _notif_svc.create_order_notification(
                        conn=conn,
                        tenant_id=cart['tenant_id'],
                        order_id=order_id,
                        payload={
                            "order_id": str(order_id),
                            "order_number": int(order_number),
                            "customer_name": customer_name or cart.get('verified_email') or 'Cliente',
                            "total_amount": float(cart_total),
                            "order_type": cart.get('order_type', 'delivery'),
                        },
                    )
                except Exception as notif_err:
                    logger.warning(f"Notification failed for order {order_id}: {notif_err}")

                order_result = {
                    "success": True,
                    "data": {
                        "order_id": str(order_id),
                        "order_number": int(order_number),
                        "total_amount": float(cart_total),
                        "order_type": cart['order_type'],
                        "pickup_pin": cart['pickup_pin'],
                        "estimated_preparation_time": estimated_preparation_time
                    }
                }

        # Transaction committed — send confirmation email outside the transaction
        # so a failure never rolls back the order.
        verified_email = cart.get('verified_email') if cart else None
        if verified_email:
            try:
                import datetime as _dt
                order_date_utc = _dt.datetime.now(_dt.timezone.utc)

                # Reconstruct scheduled_time as timezone-aware if present
                scheduled_time = cart.get('scheduled_time')
                if scheduled_time and scheduled_time.tzinfo is None:
                    scheduled_time = scheduled_time.replace(tzinfo=_dt.timezone.utc)

                await send_order_confirmation_email(
                    customer_email=verified_email,
                    order_number=int(order_number),
                    order_type=cart['order_type'],
                    order_date=order_date_utc,
                    items=items,
                    subtotal=float(cart_total),
                    delivery_address=delivery_address,
                    scheduled_time=scheduled_time,
                    delivery_instructions=cart.get('delivery_instructions'),
                    pickup_pin=cart.get('pickup_pin'),
                    order_id=str(order_id),
                )
            except Exception as email_err:
                logger.error(f"Failed to send order confirmation email for order #{order_number}: {email_err}")

        return order_result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error during checkout for cart {cart_id}: {str(e)}")
        raise APIError(f"Error al procesar el pedido: {str(e)}", status_code=500)

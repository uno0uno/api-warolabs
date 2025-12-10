"""
POS Cart Service
Handles cart persistence for POS system
"""
from typing import List, Optional
from uuid import UUID
from fastapi import Request, HTTPException
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError, APIError
import logging

logger = logging.getLogger(__name__)


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

            return {
                "success": True,
                "data": {
                    "id": str(cart_id),
                    "total_amount": float(cart_row['total_amount']),
                    "items": items,
                    "created_at": cart_row['created_at'].isoformat(),
                    "updated_at": cart_row['updated_at'].isoformat()
                }
            }

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting/creating cart: {str(e)}")
        raise APIError(f"Error getting/creating cart: {str(e)}", status_code=500)


async def get_cart_items(conn, cart_id: UUID) -> List[dict]:
    """
    Get all items in a cart with their modifiers
    """
    items_query = """
        SELECT
            ci.id,
            ci.product_id,
            ci.quantity,
            ci.unit_price,
            ci.subtotal,
            ci.notes,
            p.name as product_name
        FROM pos_cart_items ci
        JOIN product p ON ci.product_id = p.id
        WHERE ci.cart_id = $1
        ORDER BY ci.created_at
    """
    items_rows = await conn.fetch(items_query, cart_id)

    items = []
    for item_row in items_rows:
        # Get modifiers for this item
        modifiers_query = """
            SELECT
                modifier_id as id,
                modifier_name as name,
                price
            FROM pos_cart_item_modifiers
            WHERE cart_item_id = $1
            ORDER BY created_at
        """
        modifiers_rows = await conn.fetch(modifiers_query, item_row['id'])

        items.append({
            "id": str(item_row['id']),
            "product": {
                "id": str(item_row['product_id']),
                "name": item_row['product_name'],
                "image": None,  # No hay columna image_url en la tabla product
                "price": float(item_row['unit_price'])
            },
            "quantity": item_row['quantity'],
            "modifiers": [
                {
                    "id": str(mod['id']),
                    "name": mod['name'],
                    "price": float(mod['price'])
                }
                for mod in modifiers_rows
            ],
            "notes": item_row['notes'],
            "subtotal": float(item_row['subtotal'])
        })

    return items


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
                # Calculate subtotal
                modifiers_total = sum(mod['price'] for mod in modifiers)
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
                            cart_item_id, modifier_id, modifier_name, price
                        )
                        VALUES ($1, $2, $3, $4)
                    """
                    for mod in modifiers:
                        await conn.execute(
                            modifier_query,
                            item_id,
                            mod['id'],
                            mod['name'],
                            mod['price']
                        )

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
    except Exception as e:
        logger.error(f"Error adding item to cart: {str(e)}")
        raise APIError(f"Error adding item to cart: {str(e)}", status_code=500)


async def remove_item_from_cart(
    request: Request,
    cart_id: UUID,
    item_id: UUID
) -> dict:
    """
    Remove an item from cart
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                # Delete item (modifiers will cascade)
                delete_query = """
                    DELETE FROM pos_cart_items
                    WHERE id = $1 AND cart_id = $2
                """
                await conn.execute(delete_query, item_id, cart_id)

                # Update cart total
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
    cart_id: UUID
) -> dict:
    """
    Clear all items from cart
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                # Delete all items (modifiers will cascade)
                delete_query = """
                    DELETE FROM pos_cart_items
                    WHERE cart_id = $1
                """
                await conn.execute(delete_query, cart_id)

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


async def complete_pos_order(
    request: Request,
    cart_id: UUID,
    payment_method: str
) -> dict:
    """
    Complete a POS order:
    1. Create order record
    2. Copy cart items to order_items
    3. Copy modifiers to order_item_modifiers
    4. Mark cart as completed
    5. Update inventory if product controls stock
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                # 1. Verify cart exists and is active
                cart_query = """
                    SELECT id, customer_id, total_amount, tenant_id
                    FROM pos_carts
                    WHERE id = $1 AND status = 'active'
                """
                cart_row = await conn.fetchrow(cart_query, cart_id)

                if not cart_row:
                    raise APIError("Cart not found or already completed", status_code=404)

                # 2. Get cart items
                items = await get_cart_items(conn, cart_id)

                if not items:
                    raise APIError("Cannot complete order with empty cart", status_code=400)

                # 3. Create order record
                order_query = """
                    INSERT INTO orders (
                        user_id, tenant_id, customer_id, payment_method, pos_cart_id,
                        order_date, total_amount, status
                    )
                    VALUES ($1, $2, $3, $4, $5, NOW(), $6, 'completed')
                    RETURNING id, order_number, created_at
                """
                order_row = await conn.fetchrow(
                    order_query,
                    user_id,
                    tenant_id,
                    cart_row['customer_id'],
                    payment_method,
                    cart_id,
                    cart_row['total_amount']
                )
                order_id = order_row['id']
                order_number = order_row['order_number']

                logger.info(f"Created order #{order_number} from cart {cart_id}")

                # 4. Copy cart items to order_items
                for item in items:
                    # Insert order item
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
                        item['product']['id'],
                        item['quantity'],
                        item['product']['price'],
                        item['subtotal']
                    )
                    order_item_id = order_item_row['id']

                    # 5. Copy modifiers to order_item_modifiers
                    if item['modifiers']:
                        for modifier in item['modifiers']:
                            modifier_query = """
                                INSERT INTO order_item_modifiers (
                                    order_item_id, modifier_id, modifier_name, price_at_purchase
                                )
                                VALUES ($1, $2, $3, $4)
                            """
                            await conn.execute(
                                modifier_query,
                                order_item_id,
                                modifier['id'],
                                modifier['name'],
                                modifier['price']
                            )

                    # 6. Update inventory if product controls stock
                    inventory_check_query = """
                        SELECT controla_stock FROM product WHERE id = $1
                    """
                    product_row = await conn.fetchrow(inventory_check_query, item['product']['id'])

                    if product_row and product_row['controla_stock']:
                        # Get ingredients for this product
                        ingredients_query = """
                            SELECT ingredient_id, quantity, unit
                            FROM product_recipes
                            WHERE product_id = $1
                        """
                        ingredients = await conn.fetch(ingredients_query, item['product']['id'])

                        # Reduce inventory for each ingredient
                        for ingredient in ingredients:
                            update_inventory_query = """
                                UPDATE ingredient_inventory
                                SET current_quantity = current_quantity - ($1 * $2)
                                WHERE ingredient_id = $3 AND tenant_id = $4
                            """
                            await conn.execute(
                                update_inventory_query,
                                item['quantity'],  # Multiply by order quantity
                                ingredient['quantity'],  # Recipe quantity
                                ingredient['ingredient_id'],
                                tenant_id
                            )

                # 7. Mark cart as completed
                complete_cart_query = """
                    UPDATE pos_carts
                    SET status = 'completed', updated_at = NOW()
                    WHERE id = $1
                """
                await conn.execute(complete_cart_query, cart_id)

                logger.info(f"Order #{order_number} completed successfully")

                # 8. Return order details
                return {
                    "success": True,
                    "message": "Order completed successfully",
                    "data": {
                        "order_id": str(order_id),
                        "order_number": int(order_number),
                        "total_amount": float(cart_row['total_amount']),
                        "payment_method": payment_method,
                        "items_count": len(items),
                        "created_at": order_row['created_at'].isoformat()
                    }
                }

    except (AuthenticationError, APIError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error completing order: {str(e)}")
        raise APIError(f"Error completing order: {str(e)}", status_code=500)

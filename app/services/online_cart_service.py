"""
Online Cart Service
Handles cart operations for public online ordering (NO authentication required)
"""
from typing import List, Optional
from uuid import UUID
from fastapi import HTTPException
from app.database import get_db_connection
from app.core.exceptions import APIError
import logging
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

                # Add all items
                cart_total = Decimal('0')
                for item_data in items:
                    product_id = item_data['product_id']
                    quantity = item_data['quantity']
                    unit_price = Decimal(str(item_data['unit_price']))
                    modifiers = item_data.get('modifiers', [])
                    notes = item_data.get('notes')

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
    scheduled_time: Optional[str] = None,
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

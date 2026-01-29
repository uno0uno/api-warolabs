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

                # Add all items in batch
                item_ids = []
                for item in items:
                    product_id = item['product_id']
                    quantity = item['quantity']
                    unit_price = item['unit_price']
                    modifiers = item.get('modifiers', [])
                    notes = item.get('notes')

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
                    item_ids.append(str(item_id))

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
            p.name as product_name,
            p.is_resale as product_is_resale
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
            "is_resale": item_row['product_is_resale'] or False,
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

                # Calculate new subtotal
                modifiers_total = sum(mod['price'] for mod in modifiers)
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
    payment_method: str,
    customer_id: UUID
) -> dict:
    """
    Complete a POS order:
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

                # 3. Create order record (use customer_id from parameter)
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
                    customer_id,  # Use parameter instead of cart_row
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

                    # 5. Copy modifiers to order_item_modifiers and deduct ingredient inventory
                    if item['modifiers']:
                        for modifier in item['modifiers']:
                            # Get modifier quantity from cart (default 1)
                            modifier_qty = modifier.get('quantity', 1)

                            modifier_query = """
                                INSERT INTO order_item_modifiers (
                                    order_item_id, modifier_id, modifier_name, price_at_purchase, quantity
                                )
                                VALUES ($1, $2, $3, $4, $5)
                            """
                            await conn.execute(
                                modifier_query,
                                order_item_id,
                                modifier['id'],
                                modifier['name'],
                                modifier['price'],
                                modifier_qty
                            )

                            # 5b. Deduct ingredient inventory for modifier if linked
                            modifier_ingredient_query = """
                                SELECT
                                    m.ingredient_id,
                                    m.ingredient_quantity,
                                    m.ingredient_unit,
                                    i.name as ingredient_name,
                                    i.controla_inventario
                                FROM modifiers m
                                LEFT JOIN ingredients i ON m.ingredient_id = i.id
                                WHERE m.id = $1 AND m.ingredient_id IS NOT NULL
                            """
                            modifier_ingredient = await conn.fetchrow(modifier_ingredient_query, modifier['id'])

                            if modifier_ingredient and modifier_ingredient['ingredient_id'] and modifier_ingredient['ingredient_quantity']:
                                # Calculate total quantity: item_qty * modifier_qty * ingredient_quantity
                                total_deduction = (
                                    float(item['quantity']) *
                                    float(modifier_qty) *
                                    float(modifier_ingredient['ingredient_quantity'])
                                )

                                # Get current stock
                                stock_row = await conn.fetchrow(
                                    """
                                    SELECT current_stock FROM tenant_inventory
                                    WHERE ingredient_id = $1 AND tenant_id = $2
                                    """,
                                    modifier_ingredient['ingredient_id'],
                                    tenant_id
                                )

                                previous_stock = float(stock_row['current_stock']) if stock_row else 0.0
                                new_stock = max(0.0, previous_stock - total_deduction)

                                # Update or insert inventory
                                if stock_row:
                                    await conn.execute(
                                        """
                                        UPDATE tenant_inventory
                                        SET current_stock = $1, last_updated = NOW()
                                        WHERE ingredient_id = $2 AND tenant_id = $3
                                        """,
                                        new_stock,
                                        modifier_ingredient['ingredient_id'],
                                        tenant_id
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
                                        modifier_ingredient['ingredient_id'],
                                        -total_deduction
                                    )

                                # Create movement record for modifier consumption
                                await conn.execute(
                                    """
                                    INSERT INTO tenant_ingredient_movements (
                                        tenant_id, ingredient_id, movement_type,
                                        quantity_change, unit, previous_stock, new_stock,
                                        reference_table, reference_id, reason, created_by, created_at
                                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW())
                                    """,
                                    tenant_id,
                                    modifier_ingredient['ingredient_id'],
                                    'consumption',
                                    -total_deduction,
                                    modifier_ingredient['ingredient_unit'] or 'und',
                                    previous_stock,
                                    new_stock,
                                    'orders',
                                    order_id,
                                    f"Modificador {modifier['name']} ({modifier_qty}x) - Orden #{order_number}",
                                    session_context.user_id
                                )

                                logger.info(
                                    f"Modifier inventory deducted: {modifier_ingredient['ingredient_name']} "
                                    f"-{total_deduction}{modifier_ingredient['ingredient_unit']} "
                                    f"(Modifier: {modifier['name']}, Order #{order_number})"
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

                        -- Ingredients from recipe bases
                        SELECT
                            brt.ingredient_id,
                            brt.base_quantity as quantity,
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
                        # Calculate quantity to deduct (convert to float for consistency)
                        quantity_to_deduct = float(item['quantity']) * float(ingredient['quantity'])

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
                        new_stock = max(0.0, previous_stock - quantity_to_deduct)

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

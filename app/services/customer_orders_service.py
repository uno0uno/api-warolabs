"""
Customer Orders Service
Customer-scoped order queries for /api/customer/orders/* endpoints.
Authentication is handled by the get_current_customer dependency.
"""
from uuid import UUID
from app.database import get_db_connection
from app.core.exceptions import APIError
import logging

logger = logging.getLogger(__name__)


async def get_customer_order_detail(order_id: UUID, customer_id: str) -> dict:
    """
    Return full detail of a single order scoped to the authenticated customer.

    Ownership check: o.customer_id = $customer_id_from_jwt
    Returns 404 if order doesn't exist OR doesn't belong to this customer.
    Never returns 403 — don't leak order existence to wrong customers.
    """
    try:
        async with get_db_connection() as conn:
            # 1. Order header — customer-scoped
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
                    oc.pickup_pin,
                    t.name  AS restaurant_name,
                    t.slug  AS tenant_slug,
                    ap.address_line1,
                    ap.address_line2,
                    ap.city,
                    ap.delivery_notes,
                    ap.label AS address_label
                FROM orders o
                JOIN online_carts oc ON oc.id = o.online_cart_id
                JOIN tenants t ON t.id = o.tenant_id
                LEFT JOIN addresses_profile ap ON ap.id = oc.delivery_address_id
                WHERE o.id = $1
                  AND o.customer_id = $2
                  AND o.online_cart_id IS NOT NULL
            """, order_id, customer_id)

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

            # 3. Modifiers — batch fetch for all items
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

            # 4. Status history
            history_rows = await conn.fetch("""
                SELECT old_status, new_status, change_date, reason
                FROM order_status_history
                WHERE order_id = $1
                ORDER BY change_date ASC
            """, order_id)

            # 5. Compute subtotal and delivery_fee
            subtotal = sum(float(item['subtotal']) for item in item_rows)
            total_amount = float(row['total_amount'])
            delivery_fee = round(total_amount - subtotal, 2)

            # 6. Build delivery_address only for delivery orders
            delivery_address = None
            if row['address_line1']:
                delivery_address = {
                    "address_line1": row['address_line1'],
                    "address_line2": row['address_line2'],
                    "city": row['city'],
                    "delivery_notes": row['delivery_notes'],
                    "label": row['address_label'],
                }

            # 7. pickup_pin only for pickup orders
            pickup_pin = row['pickup_pin'] if row['order_type'] == 'pickup' else None

            return {
                "success": True,
                "data": {
                    "order_id": str(row['id']),
                    "order_number": int(row['order_number']),
                    "order_type": row['order_type'],
                    "status": row['status'],
                    "restaurant_name": row['restaurant_name'],
                    "tenant_slug": row['tenant_slug'],
                    "verified_email": row['verified_email'],
                    "created_at": row['order_date'].isoformat(),
                    "scheduled_time": row['scheduled_time'].isoformat() if row['scheduled_time'] else None,
                    "delivery_instructions": row['delivery_instructions'],
                    "delivery_address": delivery_address,
                    "pickup_pin": pickup_pin,
                    "payment_method": row['payment_method'],
                    "items": [
                        {
                            "product_name": item['product_name'],
                            "quantity": float(item['quantity']),
                            "unit_price": float(item['price_at_purchase']),
                            "subtotal": float(item['subtotal']),
                            "modifiers": modifiers_by_item.get(str(item['id']), []),
                        }
                        for item in item_rows
                    ],
                    "subtotal": subtotal,
                    "delivery_fee": delivery_fee,
                    "total_amount": total_amount,
                    "can_cancel": row['status'] == 'pending',
                    "status_history": [
                        {
                            "status": h['new_status'],
                            "changed_at": h['change_date'].isoformat(),
                            "note": h['reason'],
                        }
                        for h in history_rows
                    ],
                },
            }

    except APIError:
        raise
    except Exception as e:
        logger.error(f"Error getting customer order detail {order_id}: {str(e)}")
        raise APIError(f"Error getting order detail: {str(e)}", status_code=500)

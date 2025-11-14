"""
Supplier Portal Service
Allows suppliers to access their purchases and update statuses using a unique token
"""
from typing import Optional, Dict, Any, List
from uuid import UUID
from fastapi import HTTPException
from app.database import get_db_connection
from datetime import datetime

async def verify_supplier_token(token: str) -> Dict[str, Any]:
    """
    Verify if a supplier token is valid and return supplier info

    Args:
        token: The supplier's access token (UUID)

    Returns:
        Dict with supplier information

    Raises:
        HTTPException: If token is invalid
    """
    try:
        async with get_db_connection() as conn:
            supplier = await conn.fetchrow("""
                SELECT
                    id,
                    tenant_id,
                    name,
                    email,
                    phone,
                    address,
                    tax_id,
                    payment_terms,
                    access_token
                FROM tenant_suppliers
                WHERE access_token = $1
            """, UUID(token))

            if not supplier:
                raise HTTPException(
                    status_code=404,
                    detail="Token inválido o proveedor no encontrado"
                )

            return {
                "success": True,
                "supplier": {
                    "id": str(supplier['id']),
                    "tenant_id": str(supplier['tenant_id']),
                    "name": supplier['name'],
                    "email": supplier['email'],
                    "phone": supplier['phone'],
                    "address": supplier['address'],
                    "tax_id": supplier['tax_id'],
                    "payment_terms": supplier['payment_terms']
                }
            }
    except ValueError:
        raise HTTPException(status_code=400, detail="Token inválido")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Error al verificar token")


async def get_supplier_purchases(token: str, status_filter: Optional[str] = None) -> Dict[str, Any]:
    """
    Get all purchases for a supplier

    Args:
        token: The supplier's access token
        status_filter: Optional status filter (quotation, pending, confirmed, etc.)

    Returns:
        Dict with list of purchases
    """
    try:
        async with get_db_connection() as conn:
            # First verify the token and get supplier_id
            supplier = await conn.fetchrow("""
                SELECT id, tenant_id
                FROM tenant_suppliers
                WHERE access_token = $1
            """, UUID(token))

            if not supplier:
                raise HTTPException(status_code=404, detail="Token inválido")

            # Build query
            query = """
                SELECT
                    p.id,
                    p.purchase_number,
                    p.purchase_date,
                    p.delivery_date,
                    p.total_amount,
                    p.tax_amount,
                    p.status,
                    p.notes,
                    p.created_at,
                    p.updated_at
                FROM tenant_purchases p
                WHERE p.supplier_id = $1
            """

            params = [supplier['id']]

            if status_filter:
                query += " AND p.status = $2"
                params.append(status_filter)

            query += " ORDER BY p.created_at DESC"

            purchases = await conn.fetch(query, *params)

            # Fetch items for each purchase
            result_purchases = []
            for purchase in purchases:
                items = await conn.fetch("""
                    SELECT
                        i.id,
                        i.ingredient_id,
                        ing.name as ingredient_name,
                        i.quantity,
                        i.unit,
                        i.unit_cost,
                        i.total_cost,
                        i.notes
                    FROM tenant_purchase_items i
                    LEFT JOIN ingredients ing ON i.ingredient_id = ing.id
                    WHERE i.purchase_id = $1
                """, purchase['id'])

                result_purchases.append({
                    "id": str(purchase['id']),
                    "purchase_number": purchase['purchase_number'],
                    "purchase_date": purchase['purchase_date'].isoformat() if purchase['purchase_date'] else None,
                    "delivery_date": purchase['delivery_date'].isoformat() if purchase['delivery_date'] else None,
                    "total_amount": float(purchase['total_amount']) if purchase['total_amount'] else 0,
                    "tax_amount": float(purchase['tax_amount']) if purchase['tax_amount'] else 0,
                    "status": purchase['status'],
                    "notes": purchase['notes'],
                    "created_at": purchase['created_at'].isoformat() if purchase['created_at'] else None,
                    "updated_at": purchase['updated_at'].isoformat() if purchase['updated_at'] else None,
                    "items": [
                        {
                            "id": str(item['id']),
                            "ingredient_id": str(item['ingredient_id']),
                            "ingredient_name": item['ingredient_name'],
                            "quantity": float(item['quantity']),
                            "unit": item['unit'],
                            "unit_cost": float(item['unit_cost']) if item['unit_cost'] else None,
                            "total_cost": float(item['total_cost']) if item['total_cost'] else None,
                            "notes": item['notes']
                        }
                        for item in items
                    ]
                })

            return {
                "success": True,
                "data": result_purchases,
                "total": len(result_purchases)
            }

    except ValueError:
        raise HTTPException(status_code=400, detail="Token inválido")
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error getting supplier purchases: {str(e)}")
        raise HTTPException(status_code=500, detail="Error al obtener compras")


async def update_purchase_prices(
    token: str,
    purchase_id: UUID,
    items_prices: List[Dict[str, Any]],
    tax_amount: float,
    notes: Optional[str] = None
) -> Dict[str, Any]:
    """
    Allow supplier to complete quotation with prices
    Similar to complete_quotation but accessible via token

    Args:
        token: Supplier's access token
        purchase_id: Purchase ID to update
        items_prices: List of items with prices
        tax_amount: Tax amount
        notes: Optional notes
    """
    try:
        async with get_db_connection() as conn:
            # Verify token and that purchase belongs to this supplier
            supplier = await conn.fetchrow("""
                SELECT id, tenant_id
                FROM tenant_suppliers
                WHERE access_token = $1
            """, UUID(token))

            if not supplier:
                raise HTTPException(status_code=404, detail="Token inválido")

            # Verify purchase belongs to supplier and is in quotation status
            purchase = await conn.fetchrow("""
                SELECT id, status, created_by
                FROM tenant_purchases
                WHERE id = $1 AND supplier_id = $2
            """, purchase_id, supplier['id'])

            if not purchase:
                raise HTTPException(status_code=404, detail="Compra no encontrada")

            if purchase['status'] != 'quotation':
                raise HTTPException(
                    status_code=400,
                    detail="Solo se pueden completar precios en cotizaciones"
                )

            async with conn.transaction():
                # Update items with prices
                total_amount = 0
                for item_price in items_prices:
                    item_id = UUID(item_price['id'])
                    unit_cost = float(item_price['unit_cost'])

                    # Get quantity to calculate total
                    item = await conn.fetchrow("""
                        SELECT quantity FROM tenant_purchase_items WHERE id = $1
                    """, item_id)

                    if not item:
                        continue

                    total_cost = float(item['quantity']) * unit_cost
                    total_amount += total_cost

                    # Get notes - ensure it's a string or None, not empty string
                    item_notes = item_price.get('notes')
                    if not item_notes or item_notes == 'null':
                        # If no notes, update without the notes field
                        await conn.execute("""
                            UPDATE tenant_purchase_items
                            SET unit_cost = $1,
                                total_cost = $2
                            WHERE id = $3
                        """, unit_cost, total_cost, item_id)
                    else:
                        # If notes exist, update with notes
                        await conn.execute("""
                            UPDATE tenant_purchase_items
                            SET unit_cost = $1,
                                total_cost = $2,
                                notes = $3
                            WHERE id = $4
                        """, unit_cost, total_cost, str(item_notes), item_id)

                # Update purchase with totals and change status to pending
                if notes:
                    # If supplier added notes, append them
                    await conn.execute("""
                        UPDATE tenant_purchases
                        SET total_amount = $1,
                            tax_amount = $2,
                            status = 'pending',
                            notes = CONCAT(COALESCE(notes, ''), '\n[Proveedor]: ', $3),
                            updated_at = NOW()
                        WHERE id = $4
                    """, total_amount, tax_amount, str(notes), purchase_id)
                else:
                    # If no notes, just update amounts and status
                    await conn.execute("""
                        UPDATE tenant_purchases
                        SET total_amount = $1,
                            tax_amount = $2,
                            status = 'pending',
                            updated_at = NOW()
                        WHERE id = $3
                    """, total_amount, tax_amount, purchase_id)

                # Record status history (use the original creator as changed_by since supplier portal has no user session)
                await conn.execute("""
                    INSERT INTO purchase_status_history (
                        purchase_id,
                        tenant_id,
                        from_status,
                        to_status,
                        notes,
                        changed_by
                    ) VALUES ($1, $2, 'quotation', 'pending', $3, $4)
                """, purchase_id, supplier['tenant_id'],
                    'Precios completados por proveedor', purchase['created_by'])

                return {
                    "success": True,
                    "message": "Precios actualizados correctamente"
                }

    except ValueError:
        raise HTTPException(status_code=400, detail="Datos inválidos")
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error updating purchase prices: {str(e)}")
        raise HTTPException(status_code=500, detail="Error al actualizar precios")


async def ship_purchase_from_portal(
    token: str,
    purchase_id: UUID,
    tracking_number: str,
    carrier: str,
    estimated_delivery_date: Optional[str] = None,
    package_count: Optional[int] = None,
    notes: Optional[str] = None
) -> Dict[str, Any]:
    """
    Allow supplier to mark purchase as shipped via portal

    Args:
        token: Supplier's access token
        purchase_id: Purchase ID to ship
        tracking_number: Tracking number from carrier
        carrier: Shipping carrier name
        estimated_delivery_date: Optional estimated delivery date
        package_count: Optional number of packages
        notes: Optional shipping notes
    """
    try:
        async with get_db_connection() as conn:
            # Verify token and that purchase belongs to this supplier
            supplier = await conn.fetchrow("""
                SELECT id, tenant_id
                FROM tenant_suppliers
                WHERE access_token = $1
            """, UUID(token))

            if not supplier:
                raise HTTPException(status_code=404, detail="Token inválido")

            # Verify purchase belongs to supplier
            purchase = await conn.fetchrow("""
                SELECT id, status, created_by
                FROM tenant_purchases
                WHERE id = $1 AND supplier_id = $2
            """, purchase_id, supplier['id'])

            if not purchase:
                raise HTTPException(status_code=404, detail="Compra no encontrada")

            # Validate transition (confirmed -> shipped)
            if purchase['status'] not in ['confirmed', 'preparing']:
                raise HTTPException(
                    status_code=400,
                    detail=f"No se puede marcar como enviado desde el estado '{purchase['status']}'"
                )

            # Parse estimated delivery date if provided
            delivery_date = None
            if estimated_delivery_date:
                try:
                    delivery_date = datetime.fromisoformat(estimated_delivery_date.replace('Z', '+00:00'))
                except ValueError:
                    pass

            async with conn.transaction():
                # Update purchase with shipping info
                await conn.execute("""
                    UPDATE tenant_purchases
                    SET status = 'shipped',
                        tracking_number = $1,
                        carrier = $2,
                        estimated_delivery_date = $3,
                        package_count = $4,
                        shipped_at = NOW(),
                        updated_at = NOW()
                    WHERE id = $5
                """, tracking_number, carrier, delivery_date, package_count, purchase_id)

                # Create status history entry
                metadata = {
                    "tracking_number": tracking_number,
                    "carrier": carrier,
                    "package_count": package_count
                }

                import json
                await conn.execute("""
                    INSERT INTO purchase_status_history (
                        purchase_id,
                        tenant_id,
                        from_status,
                        to_status,
                        metadata,
                        notes,
                        changed_by
                    ) VALUES ($1, $2, $3, 'shipped', $4::jsonb, $5, $6)
                """, purchase_id, supplier['tenant_id'], purchase['status'],
                    json.dumps(metadata), notes or 'Marcado como enviado por proveedor',
                    purchase['created_by'])

                return {
                    "success": True,
                    "message": "Orden marcada como enviada correctamente"
                }

    except ValueError:
        raise HTTPException(status_code=400, detail="Datos inválidos")
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error shipping purchase from portal: {str(e)}")
        raise HTTPException(status_code=500, detail="Error al marcar como enviado")

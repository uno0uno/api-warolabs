"""
Supplier Portal Service
Allows suppliers to access their purchases and update statuses using a unique token
"""
from typing import Optional, Dict, Any, List
from uuid import UUID
from fastapi import HTTPException, UploadFile
from app.database import get_db_connection
from datetime import datetime
from app.services.aws_s3_service import AWSS3Service

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
                    p.updated_at,
                    p.payment_type,
                    p.payment_terms,
                    p.credit_days,
                    p.payment_due_date,
                    p.requires_advance_payment,
                    p.consolidation_group,
                    p.payment_balance
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
                    "payment_type": purchase['payment_type'],
                    "payment_terms": purchase['payment_terms'],
                    "credit_days": purchase['credit_days'],
                    "payment_due_date": purchase['payment_due_date'].isoformat() if purchase['payment_due_date'] else None,
                    "requires_advance_payment": purchase['requires_advance_payment'],
                    "consolidation_group": purchase['consolidation_group'],
                    "payment_balance": float(purchase['payment_balance']) if purchase['payment_balance'] else None,
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

        raise HTTPException(status_code=500, detail="Error al actualizar precios")

async def invoice_purchase_from_portal(
    token: str,
    purchase_id: UUID,
    document_type: str,
    invoice_number: str,
    invoice_date: str,
    invoice_amount: Optional[float] = None,
    tax_amount: Optional[float] = None,
    credit_days: Optional[int] = None,
    payment_due_date: Optional[str] = None,
    notes: Optional[str] = None,
    files: List[UploadFile] = []
) -> Dict[str, Any]:
    """
    Allow supplier to register invoice/remision via portal

    Args:
        token: Supplier's access token
        purchase_id: Purchase ID to invoice
        document_type: Type of document (remision, factura_contado, factura_credito)
        invoice_number: Invoice/remision number
        invoice_date: Date of invoice/remision
        invoice_amount: Amount (required for invoices, not for remision)
        tax_amount: Tax amount
        credit_days: Credit days for factura_credito
        payment_due_date: Payment due date
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

            # Verify purchase belongs to supplier
            purchase = await conn.fetchrow("""
                SELECT id, status, created_by
                FROM tenant_purchases
                WHERE id = $1 AND supplier_id = $2
            """, purchase_id, supplier['id'])

            if not purchase:
                raise HTTPException(status_code=404, detail="Compra no encontrada")

            # Validate transition (confirmed/preparing/paid -> invoiced)
            # For "contado" payment type: confirmed -> paid -> invoiced
            # For other types: confirmed/preparing -> invoiced
            if purchase['status'] not in ['confirmed', 'preparing', 'paid']:
                raise HTTPException(
                    status_code=400,
                    detail=f"No se puede facturar desde el estado '{purchase['status']}'"
                )

            # Validate document type
            if document_type not in ['remision', 'factura_contado', 'factura_credito']:
                raise HTTPException(status_code=400, detail="Tipo de documento inválido")

            # For invoices (not remision), amounts are required
            if document_type != 'remision' and invoice_amount is None:
                raise HTTPException(
                    status_code=400,
                    detail="El monto de factura es requerido para facturas"
                )

            # Parse dates
            try:
                inv_date = datetime.fromisoformat(invoice_date.replace('Z', '+00:00'))
            except ValueError:
                raise HTTPException(status_code=400, detail="Fecha de factura inválida")

            due_date = None
            if payment_due_date:
                try:
                    due_date = datetime.fromisoformat(payment_due_date.replace('Z', '+00:00'))
                except ValueError:
                    pass

            async with conn.transaction():
                # Update purchase with invoice info
                await conn.execute("""
                    UPDATE tenant_purchases
                    SET status = 'invoiced',
                        invoice_number = $1,
                        invoice_date = $2,
                        invoice_amount = $3,
                        tax_amount = $4,
                        payment_due_date = $5,
                        updated_at = NOW()
                    WHERE id = $6
                """, invoice_number, inv_date, invoice_amount, tax_amount or 0, due_date, purchase_id)

                # Create status history entry
                metadata = {
                    "document_type": document_type,
                    "invoice_number": invoice_number,
                    "credit_days": credit_days
                }

                import json
                doc_label = "Remisión" if document_type == 'remision' else "Factura"
                history_notes = notes or f'{doc_label} registrada por proveedor'

                await conn.execute("""
                    INSERT INTO purchase_status_history (
                        purchase_id,
                        tenant_id,
                        from_status,
                        to_status,
                        metadata,
                        notes,
                        changed_by
                    ) VALUES ($1, $2, $3, 'invoiced', $4::jsonb, $5, $6)
                """, purchase_id, supplier['tenant_id'], purchase['status'],
                    json.dumps(metadata), history_notes, purchase['created_by'])

                # Upload attachments if provided
                if files:
                    s3_service = AWSS3Service()
                    for file in files:
                        try:
                            # Upload file to S3/R2
                            s3_key = await s3_service.upload_file(
                                file_content=file.file,
                                filename=file.filename,
                                folder='purchases/attachments',
                                content_type=file.content_type
                            )

                            if s3_key:
                                # Generate presigned URL
                                file_url = await s3_service.get_presigned_url(s3_key, expiration=3600)

                                # Save attachment record to database
                                await conn.execute("""
                                    INSERT INTO purchase_attachments (
                                        tenant_id,
                                        purchase_id,
                                        path,
                                        file_name,
                                        file_size,
                                        mime_type,
                                        attachment_type,
                                        description,
                                        uploaded_by,
                                        s3_key,
                                        s3_url
                                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                                """,
                                    supplier['tenant_id'],
                                    purchase_id,
                                    s3_key,  # path (required)
                                    file.filename,
                                    file.size or 0,
                                    file.content_type or 'application/octet-stream',
                                    'invoice',
                                    f'{doc_label}: {invoice_number}',
                                    purchase['created_by'],  # Use original creator as uploader
                                    s3_key,
                                    file_url
                                )
                        except Exception as e:
                            # Continue with other files even if one fails
                            pass

                return {
                    "success": True,
                    "message": f"{doc_label} registrada correctamente"
                }

    except ValueError:
        raise HTTPException(status_code=400, detail="Datos inválidos")
    except HTTPException:
        raise
    except Exception as e:

        raise HTTPException(status_code=500, detail="Error al registrar documento")

async def ship_purchase_from_portal(
    token: str,
    purchase_id: UUID,
    tracking_number: str,
    carrier: str,
    estimated_delivery_date: Optional[str] = None,
    package_count: Optional[int] = None,
    notes: Optional[str] = None,
    files: List[UploadFile] = []
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

            # Validate transition (invoiced -> shipped)
            if purchase['status'] != 'invoiced':
                raise HTTPException(
                    status_code=400,
                    detail=f"No se puede marcar como enviado desde el estado '{purchase['status']}'. Debe facturarse primero."
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

                # Upload attachments if provided
                if files:

                    s3_service = AWSS3Service()
                    for idx, file in enumerate(files):
                        try:

                            # Upload file to S3/R2
                            s3_key = await s3_service.upload_file(
                                file_content=file.file,
                                filename=file.filename,
                                folder='purchases/attachments',
                                content_type=file.content_type
                            )

                            if s3_key:
                                # Generate presigned URL
                                file_url = await s3_service.get_presigned_url(s3_key, expiration=3600)

                                # Save attachment record to database

                                await conn.execute("""
                                    INSERT INTO purchase_attachments (
                                        tenant_id,
                                        purchase_id,
                                        path,
                                        file_name,
                                        file_size,
                                        mime_type,
                                        attachment_type,
                                        description,
                                        uploaded_by,
                                        s3_key,
                                        s3_url
                                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                                """,
                                    supplier['tenant_id'],
                                    purchase_id,
                                    s3_key,  # path (required)
                                    file.filename,
                                    file.size or 0,
                                    file.content_type or 'application/octet-stream',
                                    'shipping_label',
                                    f'Envío {tracking_number} - {carrier}',
                                    purchase['created_by'],  # Use original creator as uploader
                                    s3_key,
                                    file_url
                                )

                        except Exception as e:
                            # Continue with other files even if one fails
                            pass

                return {
                    "success": True,
                    "message": "Orden marcada como enviada correctamente"
                }

    except ValueError:
        raise HTTPException(status_code=400, detail="Datos inválidos")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Error al marcar como enviado")

async def get_supplier_invoices(
    token: str,
    document_type: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None
) -> Dict[str, Any]:
    """
    Get all invoices for a supplier with optional filters

    Args:
        token: The supplier's access token
        document_type: Optional filter by document type (factura, remision)
        start_date: Optional filter by start date (YYYY-MM-DD)
        end_date: Optional filter by end date (YYYY-MM-DD)

    Returns:
        Dict with list of invoices
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

            # Build query with filters
            query = """
                SELECT
                    p.id,
                    p.purchase_number,
                    p.purchase_date,
                    p.invoice_number,
                    p.invoice_date,
                    p.invoice_amount,
                    p.tax_amount,
                    p.total_amount,
                    p.status,
                    p.payment_type,
                    p.payment_due_date,
                    p.notes,
                    p.supplier_id,
                    s.name as supplier_name,
                    psh.metadata->>'document_type' as document_type,
                    psh.metadata->>'numero_factura_legal' as legal_invoice_number,
                    psh.metadata->>'fecha_factura_legal' as legal_invoice_date
                FROM tenant_purchases p
                LEFT JOIN tenant_suppliers s ON p.supplier_id = s.id
                LEFT JOIN LATERAL (
                    SELECT metadata
                    FROM purchase_status_history
                    WHERE purchase_id = p.id
                    AND to_status = 'invoiced'
                    ORDER BY changed_at DESC
                    LIMIT 1
                ) psh ON true
                WHERE p.supplier_id = $1
                AND p.status IN ('invoiced', 'shipped', 'received', 'paid')
            """

            params = [supplier['id']]
            param_count = 2

            # Add document_type filter
            if document_type:
                query += f" AND psh.metadata->>'document_type' = ${param_count}"
                params.append(document_type)
                param_count += 1

            # Add date filters
            if start_date:
                query += f" AND p.invoice_date >= ${param_count}"
                params.append(start_date)
                param_count += 1

            if end_date:
                query += f" AND p.invoice_date <= ${param_count}"
                params.append(end_date)
                param_count += 1

            query += " ORDER BY p.invoice_date DESC"

            # Execute query
            purchases = await conn.fetch(query, *params)

            # Format response
            invoices = []
            for purchase in purchases:
                invoices.append({
                    "id": str(purchase['id']),
                    "purchase_number": purchase['purchase_number'],
                    "purchase_date": purchase['purchase_date'].isoformat() if purchase['purchase_date'] else None,
                    "invoice_number": purchase['invoice_number'],
                    "invoice_date": purchase['invoice_date'].isoformat() if purchase['invoice_date'] else None,
                    "invoice_amount": float(purchase['invoice_amount']) if purchase['invoice_amount'] else None,
                    "tax_amount": float(purchase['tax_amount']) if purchase['tax_amount'] else 0,
                    "total_amount": float(purchase['total_amount']) if purchase['total_amount'] else 0,
                    "status": purchase['status'],
                    "payment_type": purchase['payment_type'],
                    "payment_due_date": purchase['payment_due_date'].isoformat() if purchase['payment_due_date'] else None,
                    "notes": purchase['notes'],
                    "supplier_id": str(purchase['supplier_id']),
                    "supplier_name": purchase['supplier_name'],
                    "document_type": purchase['document_type'],
                    "legal_invoice_number": purchase['legal_invoice_number'],
                    "legal_invoice_date": purchase['legal_invoice_date']
                })

            return {
                "success": True,
                "data": invoices,
                "total": len(invoices)
            }

    except ValueError:
        raise HTTPException(status_code=400, detail="Datos inválidos")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Error al obtener facturas")

async def attach_legal_invoice(
    token: str,
    purchase_ids: List[UUID],
    legal_invoice_number: str,
    legal_invoice_date: str,
    files: List[UploadFile]
) -> Dict[str, Any]:
    """
    Attach a legal invoice to multiple remisiones (delivery notes)

    This allows suppliers to link a legal invoice to multiple remisiones
    that were previously issued. The remisiones maintain their original
    document_type='remision' but get additional metadata for the legal invoice.

    Args:
        token: Supplier access token
        purchase_ids: List of purchase IDs (should be remisiones)
        legal_invoice_number: Legal invoice number
        legal_invoice_date: Legal invoice date (ISO format)
        files: Invoice document files

    Returns:
        Dict with success status
    """
    print(f"DEBUG attach_legal_invoice called with:")
    print(f"  token: {token}")
    print(f"  purchase_ids: {purchase_ids}")
    print(f"  legal_invoice_number: {legal_invoice_number}")
    print(f"  legal_invoice_date: {legal_invoice_date}")
    print(f"  files: {len(files) if files else 0}")

    try:
        async with get_db_connection() as conn:
            # Verify supplier token
            supplier = await conn.fetchrow("""
                SELECT id, tenant_id, name
                FROM tenant_suppliers
                WHERE access_token = $1
            """, UUID(token))

            if not supplier:
                raise HTTPException(status_code=404, detail="Token inválido")

            # Verify all purchases belong to this supplier and are remisiones
            purchases_check = await conn.fetch("""
                SELECT p.id, p.purchase_number, p.created_by, psh.metadata->>'document_type' as document_type
                FROM tenant_purchases p
                LEFT JOIN LATERAL (
                    SELECT metadata
                    FROM purchase_status_history
                    WHERE purchase_id = p.id
                    AND to_status = 'invoiced'
                    ORDER BY changed_at DESC
                    LIMIT 1
                ) psh ON true
                WHERE p.id = ANY($1)
                AND p.supplier_id = $2
            """, purchase_ids, supplier['id'])

            if len(purchases_check) != len(purchase_ids):
                raise HTTPException(
                    status_code=400,
                    detail="Algunas órdenes no pertenecen a este proveedor o no existen"
                )

            # Check if all are remisiones
            non_remisiones = [p for p in purchases_check if p['document_type'] != 'remision']
            if non_remisiones:
                raise HTTPException(
                    status_code=400,
                    detail="Solo se puede adjuntar factura legal a remisiones"
                )

            # Upload files FIRST (outside transaction) to avoid S3 initialization issues
            uploaded_files = []
            if files:
                s3_service = AWSS3Service()
                for file in files:
                    if file.filename:
                        try:
                            file_content = await file.read()
                            file_key = f"invoices/{supplier['tenant_id']}/legal/{legal_invoice_number}/{file.filename}"
                            # Use the new upload_file_with_key method that accepts a specific key
                            uploaded_key = await s3_service.upload_file_with_key(
                                file_content,
                                file_key,
                                file.content_type
                            )
                            if uploaded_key:
                                uploaded_files.append({
                                    'filename': file.filename,
                                    'key': uploaded_key,
                                    'content_type': file.content_type,
                                    'size': len(file_content)
                                })
                        except Exception as e:
                            print(f"Warning: Failed to upload file {file.filename}: {str(e)}")
                            # Continue with other files even if one fails
                            pass

            # Now do database transaction
            async with conn.transaction():
                # Update metadata in purchase_status_history for each purchase
                for purchase_id in purchase_ids:
                    # First check if a status history record exists for 'invoiced'
                    history_record = await conn.fetchrow("""
                        SELECT id, metadata
                        FROM purchase_status_history
                        WHERE purchase_id = $1
                        AND to_status = 'invoiced'
                        ORDER BY changed_at DESC
                        LIMIT 1
                    """, purchase_id)

                    if history_record:
                        # Update existing record
                        await conn.execute("""
                            UPDATE purchase_status_history
                            SET metadata = jsonb_set(
                                jsonb_set(
                                    COALESCE(metadata, '{}'::jsonb),
                                    '{numero_factura_legal}',
                                    to_jsonb($2::text)
                                ),
                                '{fecha_factura_legal}',
                                to_jsonb($3::text)
                            )
                            WHERE id = $1
                        """, history_record['id'], legal_invoice_number, legal_invoice_date)
                    else:
                        # If no history record exists, we need to create one or update the purchase directly
                        # For now, let's just log a warning and continue
                        print(f"Warning: No invoiced status history found for purchase {purchase_id}")

                # Create attachment records for uploaded files
                # Use purchase created_by as uploaded_by (original creator of purchase order)
                for uploaded_file in uploaded_files:
                    for purchase_record in purchases_check:
                        await conn.execute("""
                            INSERT INTO purchase_attachments
                            (purchase_id, tenant_id, path, file_name, file_size, s3_key, mime_type, attachment_type, related_status, description, uploaded_by, uploaded_at)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW())
                        """,
                            purchase_record['id'],
                            supplier['tenant_id'],
                            uploaded_file['key'],  # path (S3 key)
                            uploaded_file['filename'],
                            uploaded_file['size'],
                            uploaded_file['key'],  # s3_key
                            uploaded_file['content_type'],
                            'invoice',  # Valid attachment type
                            'invoiced',
                            f'Factura Legal {legal_invoice_number}',  # Description to identify it's a legal invoice
                            purchase_record['created_by']  # Use original purchase creator as uploader
                        )

            return {
                "success": True,
                "message": f"Factura legal {legal_invoice_number} adjuntada a {len(purchase_ids)} remision(es)",
                "affected_purchases": len(purchase_ids),
                "files_uploaded": len(uploaded_files)
            }

    except ValueError as e:
        print(f"ERROR ValueError: {str(e)}")
        raise HTTPException(status_code=400, detail="Datos inválidos")
    except HTTPException:
        raise
    except Exception as e:
        print(f"ERROR Exception: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error al adjuntar factura legal: {str(e)}")

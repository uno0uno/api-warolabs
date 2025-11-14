from typing import Optional, Dict, Any
from uuid import UUID
from fastapi import Request, Response, HTTPException
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError
from app.models.purchase import (
    Purchase,
    PurchaseCreate,
    PurchaseUpdate,
    PurchaseItem,
    PurchaseResponse,
    PurchasesListResponse
)
from app.services.email_helpers import send_quotation_email

async def get_purchases_list(
    request: Request,
    response: Response,
    page: int = 1,
    limit: int = 50,
    search: Optional[str] = None,
    status: Optional[str] = None,
    supplier_id: Optional[UUID] = None
) -> PurchasesListResponse:
    """
    Get purchases list with tenant isolation
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Build query with tenant isolation
            base_query = """
                SELECT
                    id,
                    tenant_id,
                    supplier_id,
                    purchase_number,
                    purchase_date,
                    delivery_date,
                    total_amount,
                    tax_amount,
                    status,
                    invoice_number,
                    notes,
                    created_by,
                    created_at,
                    updated_at
                FROM tenant_purchases
                WHERE tenant_id = $1
            """

            count_query = """
                SELECT COUNT(*) as total
                FROM tenant_purchases
                WHERE tenant_id = $1
            """

            params = [tenant_id]
            param_count = 2

            # Add filters
            if search:
                base_query += f" AND (LOWER(purchase_number) LIKE LOWER(${param_count}) OR LOWER(invoice_number) LIKE LOWER(${param_count}))"
                count_query += f" AND (LOWER(purchase_number) LIKE LOWER(${param_count}) OR LOWER(invoice_number) LIKE LOWER(${param_count}))"
                params.append(f"%{search}%")
                param_count += 1

            if status:
                base_query += f" AND LOWER(status) = LOWER(${param_count})"
                count_query += f" AND LOWER(status) = LOWER(${param_count})"
                params.append(status)
                param_count += 1

            if supplier_id:
                base_query += f" AND supplier_id = ${param_count}"
                count_query += f" AND supplier_id = ${param_count}"
                params.append(supplier_id)
                param_count += 1

            # Add pagination
            offset = (page - 1) * limit
            base_query += f" ORDER BY created_at DESC LIMIT ${param_count} OFFSET ${param_count + 1}"
            params.extend([limit, offset])

            # Execute queries
            purchases_data = await conn.fetch(base_query, *params)
            count_result = await conn.fetchrow(count_query, *params[:-2])

            # Convert to models and fetch items
            purchases = []
            for row in purchases_data:
                # Fetch items for this purchase
                items_data = await conn.fetch("""
                    SELECT
                        id,
                        purchase_id,
                        ingredient_id,
                        quantity,
                        unit,
                        unit_cost,
                        total_cost,
                        expiry_date,
                        batch_number,
                        notes,
                        created_at
                    FROM tenant_purchase_items
                    WHERE purchase_id = $1
                """, row['id'])

                items = [PurchaseItem(**item) for item in items_data]

                purchase = Purchase(
                    id=row['id'],
                    tenant_id=row['tenant_id'],
                    supplier_id=row['supplier_id'],
                    purchase_number=row['purchase_number'],
                    purchase_date=row['purchase_date'],
                    delivery_date=row['delivery_date'],
                    total_amount=row['total_amount'],
                    tax_amount=row['tax_amount'],
                    status=row['status'],
                    invoice_number=row['invoice_number'],
                    notes=row['notes'],
                    created_by=row['created_by'],
                    created_at=row['created_at'],
                    updated_at=row['updated_at'],
                    items=items
                )
                purchases.append(purchase)

            return PurchasesListResponse(
                data=purchases,
                total=count_result['total'],
                page=page,
                limit=limit
            )

    except AuthenticationError:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Error interno del servidor")

async def get_purchase_by_id(
    request: Request,
    response: Response,
    purchase_id: UUID
) -> PurchaseResponse:
    """
    Get a specific purchase by ID with tenant isolation
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            purchase_data = await conn.fetchrow("""
                SELECT
                    id,
                    tenant_id,
                    supplier_id,
                    purchase_number,
                    purchase_date,
                    delivery_date,
                    total_amount,
                    tax_amount,
                    status,
                    invoice_number,
                    notes,
                    created_by,
                    created_at,
                    updated_at
                FROM tenant_purchases
                WHERE id = $1 AND tenant_id = $2
            """, purchase_id, tenant_id)

            if not purchase_data:
                raise HTTPException(status_code=404, detail="Purchase not found")

            # Fetch items
            items_data = await conn.fetch("""
                SELECT
                    id,
                    purchase_id,
                    ingredient_id,
                    quantity,
                    unit,
                    unit_cost,
                    total_cost,
                    expiry_date,
                    batch_number,
                    notes,
                    created_at
                FROM tenant_purchase_items
                WHERE purchase_id = $1
            """, purchase_id)

            items = [PurchaseItem(**item) for item in items_data]

            purchase = Purchase(
                id=purchase_data['id'],
                tenant_id=purchase_data['tenant_id'],
                supplier_id=purchase_data['supplier_id'],
                purchase_number=purchase_data['purchase_number'],
                purchase_date=purchase_data['purchase_date'],
                delivery_date=purchase_data['delivery_date'],
                total_amount=purchase_data['total_amount'],
                tax_amount=purchase_data['tax_amount'],
                status=purchase_data['status'],
                invoice_number=purchase_data['invoice_number'],
                notes=purchase_data['notes'],
                created_by=purchase_data['created_by'],
                created_at=purchase_data['created_at'],
                updated_at=purchase_data['updated_at'],
                items=items
            )

            return PurchaseResponse(data=purchase)

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Error interno del servidor")

async def create_purchase(
    request: Request,
    response: Response,
    purchase_data: PurchaseCreate
) -> PurchaseResponse:
    """
    Create a new purchase with tenant isolation
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                # Generate purchase number automatically: WR-YYYY-NNNN
                from datetime import datetime
                current_year = datetime.now().year

                # Get the last purchase number for this year and tenant
                last_purchase = await conn.fetchrow("""
                    SELECT purchase_number
                    FROM tenant_purchases
                    WHERE tenant_id = $1
                        AND purchase_number LIKE $2
                    ORDER BY created_at DESC
                    LIMIT 1
                """, tenant_id, f'WR-{current_year}-%')

                # Extract sequence number and increment
                if last_purchase and last_purchase['purchase_number']:
                    # Extract NNNN from WR-YYYY-NNNN
                    last_number = int(last_purchase['purchase_number'].split('-')[-1])
                    next_number = last_number + 1
                else:
                    next_number = 1

                # Generate new purchase number: WR-YYYY-NNNN
                purchase_number = f"WR-{current_year}-{next_number:04d}"

                # Use invoice number from request (user entered manually)
                invoice_number = purchase_data.invoice_number

                # Insert new purchase
                new_purchase = await conn.fetchrow("""
                    INSERT INTO tenant_purchases (
                        tenant_id,
                        supplier_id,
                        purchase_number,
                        purchase_date,
                        delivery_date,
                        total_amount,
                        tax_amount,
                        status,
                        invoice_number,
                        notes,
                        created_by
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                    RETURNING
                        id,
                        tenant_id,
                        supplier_id,
                        purchase_number,
                        purchase_date,
                        delivery_date,
                        total_amount,
                        tax_amount,
                        status,
                        invoice_number,
                        notes,
                        created_by,
                        created_at,
                        updated_at
                """,
                    tenant_id,
                    purchase_data.supplier_id,
                    purchase_number,  # Auto-generated
                    purchase_data.purchase_date,
                    purchase_data.delivery_date,
                    purchase_data.total_amount,
                    purchase_data.tax_amount,
                    purchase_data.status,
                    invoice_number,  # Auto-generated if not provided
                    purchase_data.notes,
                    user_id
                )

                purchase_id = new_purchase['id']

                # Insert purchase items
                items = []
                for item_data in purchase_data.items:
                    # Validate ingredient exists and unit matches (frontend should convert to base unit)
                    ingredient = await conn.fetchrow("""
                        SELECT unit FROM ingredients WHERE id = $1
                    """, item_data.ingredient_id)

                    if not ingredient:
                        raise HTTPException(status_code=400, detail=f"Ingrediente no encontrado: {item_data.ingredient_id}")

                    # Verify unit matches ingredient's base unit (data should come converted from frontend)
                    if item_data.unit != ingredient['unit']:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Error de conversión: se esperaba '{ingredient['unit']}' pero se recibió '{item_data.unit}'"
                        )

                    # Calculate total_cost only if unit_cost is provided (not for quotations)
                    total_cost = None
                    if item_data.unit_cost is not None:
                        total_cost = item_data.total_cost or (item_data.quantity * item_data.unit_cost)

                    new_item = await conn.fetchrow("""
                        INSERT INTO tenant_purchase_items (
                            purchase_id,
                            ingredient_id,
                            quantity,
                            unit,
                            unit_cost,
                            total_cost,
                            expiry_date,
                            batch_number,
                            notes
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                        RETURNING
                            id,
                            purchase_id,
                            ingredient_id,
                            quantity,
                            unit,
                            unit_cost,
                            total_cost,
                            expiry_date,
                            batch_number,
                            notes,
                            created_at
                    """,
                        purchase_id,
                        item_data.ingredient_id,
                        item_data.quantity,
                        item_data.unit,
                        item_data.unit_cost,
                        total_cost,
                        item_data.expiry_date,
                        item_data.batch_number,
                        item_data.notes
                    )
                    items.append(PurchaseItem(**new_item))

                purchase = Purchase(
                    id=new_purchase['id'],
                    tenant_id=new_purchase['tenant_id'],
                    supplier_id=new_purchase['supplier_id'],
                    purchase_number=new_purchase['purchase_number'],
                    purchase_date=new_purchase['purchase_date'],
                    delivery_date=new_purchase['delivery_date'],
                    total_amount=new_purchase['total_amount'],
                    tax_amount=new_purchase['tax_amount'],
                    status=new_purchase['status'],
                    invoice_number=new_purchase['invoice_number'],
                    notes=new_purchase['notes'],
                    created_by=new_purchase['created_by'],
                    created_at=new_purchase['created_at'],
                    updated_at=new_purchase['updated_at'],
                    items=items
                )

                # If this is a quotation, send email to supplier
                if new_purchase['status'] == 'quotation':
                    try:
                        # Fetch tenant site information from tenant_sites
                        tenant_info = await conn.fetchrow("""
                            SELECT site FROM tenant_sites WHERE tenant_id = $1 AND is_active = true LIMIT 1
                        """, tenant_id)

                        # Fetch supplier information including access token
                        supplier = await conn.fetchrow("""
                            SELECT name, email, access_token
                            FROM tenant_suppliers
                            WHERE id = $1 AND tenant_id = $2
                        """, purchase_data.supplier_id, tenant_id)

                        if supplier and supplier['email']:
                            # Fetch ingredient names for email
                            items_with_names = []
                            for item in items:
                                ingredient = await conn.fetchrow("""
                                    SELECT name FROM ingredients WHERE id = $1
                                """, item.ingredient_id)
                                items_with_names.append({
                                    'ingredient_name': ingredient['name'] if ingredient else 'Producto',
                                    'quantity': item.quantity,
                                    'unit': item.unit
                                })

                            # Send quotation email with portal link
                            await send_quotation_email(
                                supplier_email=supplier['email'],
                                supplier_name=supplier['name'],
                                purchase_number=new_purchase['purchase_number'],
                                purchase_date=new_purchase['purchase_date'],
                                delivery_date=new_purchase['delivery_date'],
                                items=items_with_names,
                                notes=purchase_data.notes,
                                supplier_token=str(supplier['access_token']) if supplier['access_token'] else None,
                                tenant_site=tenant_info['site'] if tenant_info else None
                            )
                    except Exception as email_error:
                        # Log error but don't fail the purchase creation
                        print(f"Error sending quotation email: {str(email_error)}")

                return PurchaseResponse(data=purchase)

    except AuthenticationError:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Error interno del servidor")

async def update_purchase(
    request: Request,
    response: Response,
    purchase_id: UUID,
    purchase_data: PurchaseUpdate
) -> PurchaseResponse:
    """
    Update an existing purchase with tenant isolation
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                # Verify purchase exists and belongs to tenant
                existing_purchase = await conn.fetchrow("""
                    SELECT id FROM tenant_purchases
                    WHERE id = $1 AND tenant_id = $2
                """, purchase_id, tenant_id)

                if not existing_purchase:
                    raise HTTPException(status_code=404, detail="Purchase not found")

                # Build update query dynamically
                update_fields = []
                params = [purchase_id, tenant_id]
                param_count = 3

                for field, value in purchase_data.dict(exclude_unset=True, exclude={'items'}).items():
                    update_fields.append(f"{field} = ${param_count}")
                    params.append(value)
                    param_count += 1

                if update_fields:
                    # Add updated_at
                    update_fields.append(f"updated_at = NOW()")

                    update_query = f"""
                        UPDATE tenant_purchases
                        SET {', '.join(update_fields)}
                        WHERE id = $1 AND tenant_id = $2
                    """

                    await conn.execute(update_query, *params)

                # Update items if provided
                if purchase_data.items is not None:
                    # Delete existing items
                    await conn.execute("""
                        DELETE FROM tenant_purchase_items
                        WHERE purchase_id = $1
                    """, purchase_id)

                    # Insert new items
                    for item_data in purchase_data.items:
                        # Validate unit matches ingredient's unit
                        ingredient = await conn.fetchrow("""
                            SELECT unit FROM ingredients WHERE id = $1
                        """, item_data.ingredient_id)

                        if not ingredient:
                            raise HTTPException(status_code=400, detail=f"Ingrediente no encontrado: {item_data.ingredient_id}")

                        if item_data.unit != ingredient['unit']:
                            raise HTTPException(
                                status_code=400,
                                detail=f"Unidad incorrecta. El ingrediente usa '{ingredient['unit']}' pero se envió '{item_data.unit}'"
                            )

                        # Calculate total_cost only if unit_cost is provided (not for quotations)
                        total_cost = None
                        if item_data.unit_cost is not None:
                            total_cost = item_data.total_cost or (item_data.quantity * item_data.unit_cost)

                        await conn.execute("""
                            INSERT INTO tenant_purchase_items (
                                purchase_id,
                                ingredient_id,
                                quantity,
                                unit,
                                unit_cost,
                                total_cost,
                                expiry_date,
                                batch_number,
                                notes
                            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                        """,
                            purchase_id,
                            item_data.ingredient_id,
                            item_data.quantity,
                            item_data.unit,
                            item_data.unit_cost,
                            total_cost,
                            item_data.expiry_date,
                            item_data.batch_number,
                            item_data.notes
                        )

                # Fetch updated purchase
                return await get_purchase_by_id(request, response, purchase_id)

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Error interno del servidor")

async def delete_purchase(
    request: Request,
    response: Response,
    purchase_id: UUID
) -> Dict[str, Any]:
    """
    Delete a purchase with tenant isolation
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                # Verify purchase exists and belongs to tenant
                existing_purchase = await conn.fetchrow("""
                    SELECT id FROM tenant_purchases
                    WHERE id = $1 AND tenant_id = $2
                """, purchase_id, tenant_id)

                if not existing_purchase:
                    raise HTTPException(status_code=404, detail="Purchase not found")

                # Delete items first (foreign key constraint)
                await conn.execute("""
                    DELETE FROM tenant_purchase_items
                    WHERE purchase_id = $1
                """, purchase_id)

                # Delete purchase
                await conn.execute("""
                    DELETE FROM tenant_purchases
                    WHERE id = $1 AND tenant_id = $2
                """, purchase_id, tenant_id)

                return {
                    "success": True,
                    "message": "Purchase deleted successfully"
                }

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Error interno del servidor")

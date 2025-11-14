"""
Purchase Tracking Service
Handles status transitions, attachments, and history for purchase orders
"""

from typing import Optional, List, Dict, Any
from uuid import UUID
from datetime import datetime
from fastapi import Request, Response, HTTPException
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError
from app.models.purchase import (
    PurchaseStatusHistory,
    PurchaseStatusHistoryCreate,
    PurchaseAttachment,
    PurchaseAttachmentCreate,
    PurchaseStatus,
    ConfirmPurchaseData,
    ShipPurchaseData,
    ReceivePurchaseData,
    VerifyPurchaseData,
    InvoicePurchaseData,
    PayPurchaseData,
    CancelPurchaseData,
    StatusHistoryResponse,
    AttachmentsResponse,
)
from app.services.email_helpers import send_purchase_status_notification

# =============================================================================
# STATE TRANSITION RULES
# =============================================================================

STATE_TRANSITIONS = {
    'quotation': ['pending', 'cancelled'],  # Quotation can be completed (with prices) or cancelled
    'pending': ['confirmed', 'cancelled'],
    'confirmed': ['preparing', 'shipped', 'cancelled'],  # Can skip preparing and go directly to shipped
    'preparing': ['shipped', 'cancelled'],
    'shipped': ['received', 'partially_received', 'overdue'],
    'partially_received': ['received', 'overdue'],
    'received': ['verified'],
    'verified': ['invoiced'],
    'invoiced': ['paid'],
    'paid': [],  # Final state
    'cancelled': [],  # Final state
    'overdue': ['shipped', 'received', 'cancelled']  # Can resume flow
}

def validate_state_transition(from_status: str, to_status: str) -> bool:
    """Validate if a state transition is allowed"""
    allowed_transitions = STATE_TRANSITIONS.get(from_status, [])
    return to_status in allowed_transitions

# =============================================================================
# STATUS HISTORY FUNCTIONS
# =============================================================================

async def get_purchase_status_history(
    request: Request,
    response: Response,
    purchase_id: UUID
) -> StatusHistoryResponse:
    """Get full status history for a purchase"""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Verify purchase belongs to tenant
            purchase = await conn.fetchrow(
                "SELECT id FROM tenant_purchases WHERE id = $1 AND tenant_id = $2",
                purchase_id, tenant_id
            )

            if not purchase:
                raise HTTPException(status_code=404, detail="Purchase not found")

            # Get status history
            history_data = await conn.fetch("""
                SELECT
                    id,
                    purchase_id,
                    tenant_id,
                    from_status,
                    to_status,
                    changed_by,
                    changed_at,
                    metadata,
                    notes,
                    created_at
                FROM purchase_status_history
                WHERE purchase_id = $1
                ORDER BY changed_at DESC
            """, purchase_id)

            # Parse metadata from JSON string to dict
            import json
            history = []
            for row in history_data:
                row_dict = dict(row)
                # Parse metadata if it's a string
                if row_dict.get('metadata') and isinstance(row_dict['metadata'], str):
                    try:
                        row_dict['metadata'] = json.loads(row_dict['metadata'])
                    except json.JSONDecodeError:
                        row_dict['metadata'] = {}
                history.append(PurchaseStatusHistory(**row_dict))

            return StatusHistoryResponse(data=history)

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error fetching status history: {e}")
        raise HTTPException(status_code=500, detail="Error interno del servidor")

async def create_status_history_entry(
    conn,
    purchase_id: UUID,
    tenant_id: UUID,
    from_status: Optional[str],
    to_status: str,
    changed_by: UUID,
    metadata: Optional[dict] = None,
    notes: Optional[str] = None
):
    """Create a status history entry"""
    import json

    # Convert metadata dict to JSON string for JSONB column
    metadata_json = json.dumps(metadata) if metadata else '{}'

    await conn.execute("""
        INSERT INTO purchase_status_history (
            purchase_id,
            tenant_id,
            from_status,
            to_status,
            changed_by,
            metadata,
            notes
        ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
    """, purchase_id, tenant_id, from_status, to_status, changed_by,
    metadata_json, notes)

# =============================================================================
# ATTACHMENT FUNCTIONS
# =============================================================================

async def get_purchase_attachments(
    request: Request,
    response: Response,
    purchase_id: UUID
) -> AttachmentsResponse:
    """Get all attachments for a purchase"""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Verify purchase belongs to tenant
            purchase = await conn.fetchrow(
                "SELECT id FROM tenant_purchases WHERE id = $1 AND tenant_id = $2",
                purchase_id, tenant_id
            )

            if not purchase:
                raise HTTPException(status_code=404, detail="Purchase not found")

            # Get attachments
            attachments_data = await conn.fetch("""
                SELECT
                    id,
                    purchase_id,
                    tenant_id,
                    path,
                    file_name,
                    file_size,
                    mime_type,
                    attachment_type,
                    related_status,
                    description,
                    uploaded_by,
                    uploaded_at,
                    created_at
                FROM purchase_attachments
                WHERE purchase_id = $1
                ORDER BY uploaded_at DESC
            """, purchase_id)

            attachments = [PurchaseAttachment(**dict(row)) for row in attachments_data]

            return AttachmentsResponse(data=attachments)

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error fetching attachments: {e}")
        raise HTTPException(status_code=500, detail="Error interno del servidor")

async def create_purchase_attachment(
    request: Request,
    response: Response,
    attachment_data: PurchaseAttachmentCreate
) -> Dict[str, Any]:
    """Create a new attachment for a purchase"""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Verify purchase belongs to tenant
            purchase = await conn.fetchrow(
                "SELECT id FROM tenant_purchases WHERE id = $1 AND tenant_id = $2",
                attachment_data.purchase_id, tenant_id
            )

            if not purchase:
                raise HTTPException(status_code=404, detail="Purchase not found")

            # Create attachment
            new_attachment = await conn.fetchrow("""
                INSERT INTO purchase_attachments (
                    purchase_id,
                    tenant_id,
                    path,
                    file_name,
                    file_size,
                    mime_type,
                    attachment_type,
                    related_status,
                    description,
                    uploaded_by
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                RETURNING *
            """,
                attachment_data.purchase_id,
                tenant_id,
                attachment_data.path,
                attachment_data.file_name,
                attachment_data.file_size,
                attachment_data.mime_type,
                attachment_data.attachment_type,
                attachment_data.related_status,
                attachment_data.description,
                user_id
            )

            return {
                "success": True,
                "data": PurchaseAttachment(**dict(new_attachment))
            }

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error creating attachment: {e}")
        raise HTTPException(status_code=500, detail="Error interno del servidor")

# =============================================================================
# STATE TRANSITION FUNCTIONS
# =============================================================================

async def transition_to_confirmed(
    request: Request,
    response: Response,
    purchase_id: UUID,
    data: ConfirmPurchaseData
) -> Dict[str, Any]:
    """Transition purchase to confirmed state"""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                # Get current purchase
                purchase = await conn.fetchrow("""
                    SELECT id, status FROM tenant_purchases
                    WHERE id = $1 AND tenant_id = $2
                """, purchase_id, tenant_id)

                if not purchase:
                    raise HTTPException(status_code=404, detail="Purchase not found")

                # Validate transition
                if not validate_state_transition(purchase['status'], 'confirmed'):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Cannot transition from {purchase['status']} to confirmed"
                    )

                # Update purchase
                await conn.execute("""
                    UPDATE tenant_purchases
                    SET
                        status = 'confirmed',
                        confirmation_number = $1,
                        estimated_delivery_date = $2,
                        confirmed_at = NOW(),
                        updated_at = NOW()
                    WHERE id = $3
                """, data.confirmation_number, data.estimated_delivery_date, purchase_id)

                # Create history entry (trigger will also create one)
                await create_status_history_entry(
                    conn, purchase_id, tenant_id,
                    purchase['status'], 'confirmed', user_id,
                    {
                        "confirmation_number": data.confirmation_number,
                        "estimated_delivery_date": data.estimated_delivery_date.isoformat() if data.estimated_delivery_date else None
                    },
                    data.notes
                )

                # Send email notification to supplier
                try:
                    # Fetch purchase details, supplier info, and tenant site
                    purchase_info = await conn.fetchrow("""
                        SELECT tp.purchase_number, ts.name as supplier_name, ts.email as supplier_email,
                               ts.access_token as supplier_token, tsi.site as tenant_site
                        FROM tenant_purchases tp
                        JOIN tenant_suppliers ts ON tp.supplier_id = ts.id
                        LEFT JOIN tenant_sites tsi ON tp.tenant_id = tsi.tenant_id AND tsi.is_active = true
                        WHERE tp.id = $1
                        LIMIT 1
                    """, purchase_id)

                    if purchase_info and purchase_info['supplier_email']:
                        await send_purchase_status_notification(
                            supplier_email=purchase_info['supplier_email'],
                            supplier_name=purchase_info['supplier_name'],
                            purchase_number=purchase_info['purchase_number'],
                            status='confirmed',
                            notes=data.notes,
                            metadata={
                                "confirmation_number": data.confirmation_number,
                                "estimated_delivery_date": data.estimated_delivery_date.strftime('%d de %B de %Y') if data.estimated_delivery_date else None
                            },
                            supplier_token=str(purchase_info['supplier_token']) if purchase_info['supplier_token'] else None,
                            tenant_site=purchase_info['tenant_site']
                        )
                except Exception as email_error:
                    print(f"Error sending confirmation email: {str(email_error)}")

                return {"success": True, "message": "Purchase confirmed successfully"}

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error confirming purchase: {e}")
        raise HTTPException(status_code=500, detail="Error interno del servidor")

async def transition_to_shipped(
    request: Request,
    response: Response,
    purchase_id: UUID,
    data: ShipPurchaseData
) -> Dict[str, Any]:
    """Transition purchase to shipped state"""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                # Get current purchase
                purchase = await conn.fetchrow("""
                    SELECT id, status FROM tenant_purchases
                    WHERE id = $1 AND tenant_id = $2
                """, purchase_id, tenant_id)

                if not purchase:
                    raise HTTPException(status_code=404, detail="Purchase not found")

                # Validate transition
                current_status = purchase['status']
                print(f"DEBUG: Attempting transition from '{current_status}' to 'shipped'")
                print(f"DEBUG: Valid transitions from '{current_status}': {STATE_TRANSITIONS.get(current_status, [])}")

                if not validate_state_transition(current_status, 'shipped'):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Cannot transition from '{current_status}' to 'shipped'. Valid next states: {STATE_TRANSITIONS.get(current_status, [])}"
                    )

                # Update purchase
                await conn.execute("""
                    UPDATE tenant_purchases
                    SET
                        status = 'shipped',
                        tracking_number = $1,
                        carrier = $2,
                        estimated_delivery_date = $3,
                        package_count = $4,
                        shipped_at = NOW(),
                        updated_at = NOW()
                    WHERE id = $5
                """, data.tracking_number, data.carrier, data.estimated_delivery_date,
                data.package_count, purchase_id)

                # Create history entry
                await create_status_history_entry(
                    conn, purchase_id, tenant_id,
                    purchase['status'], 'shipped', user_id,
                    {
                        "tracking_number": data.tracking_number,
                        "carrier": data.carrier,
                        "package_count": data.package_count
                    },
                    data.notes
                )

                # Send email notification to supplier
                try:
                    purchase_info = await conn.fetchrow("""
                        SELECT tp.purchase_number, ts.name as supplier_name, ts.email as supplier_email,
                               ts.access_token as supplier_token, tsi.site as tenant_site
                        FROM tenant_purchases tp
                        JOIN tenant_suppliers ts ON tp.supplier_id = ts.id
                        LEFT JOIN tenant_sites tsi ON tp.tenant_id = tsi.tenant_id AND tsi.is_active = true
                        WHERE tp.id = $1
                        LIMIT 1
                    """, purchase_id)

                    if purchase_info and purchase_info['supplier_email']:
                        await send_purchase_status_notification(
                            supplier_email=purchase_info['supplier_email'],
                            supplier_name=purchase_info['supplier_name'],
                            purchase_number=purchase_info['purchase_number'],
                            status='shipped',
                            notes=data.notes,
                            metadata={
                                "tracking_number": data.tracking_number,
                                "carrier": data.carrier,
                                "estimated_delivery_date": data.estimated_delivery_date.strftime('%d de %B de %Y') if data.estimated_delivery_date else None,
                                "package_count": data.package_count
                            },
                            supplier_token=str(purchase_info['supplier_token']) if purchase_info['supplier_token'] else None,
                            tenant_site=purchase_info['tenant_site']
                        )
                except Exception as email_error:
                    print(f"Error sending shipped email: {str(email_error)}")

                return {"success": True, "message": "Purchase marked as shipped"}

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error shipping purchase: {e}")
        raise HTTPException(status_code=500, detail="Error interno del servidor")

async def transition_to_received(
    request: Request,
    response: Response,
    purchase_id: UUID,
    data: ReceivePurchaseData
) -> Dict[str, Any]:
    """Transition purchase to received state"""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                # Get current purchase
                purchase = await conn.fetchrow("""
                    SELECT id, status FROM tenant_purchases
                    WHERE id = $1 AND tenant_id = $2
                """, purchase_id, tenant_id)

                if not purchase:
                    raise HTTPException(status_code=404, detail="Purchase not found")

                # Determine target status
                target_status = 'partially_received' if data.partial else 'received'

                # Validate transition
                if not validate_state_transition(purchase['status'], target_status):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Cannot transition from {purchase['status']} to {target_status}"
                    )

                # Update purchase
                await conn.execute("""
                    UPDATE tenant_purchases
                    SET
                        status = $1,
                        package_condition = $2,
                        received_by = $3,
                        received_at = NOW(),
                        updated_at = NOW()
                    WHERE id = $4
                """, target_status, data.package_condition, user_id, purchase_id)

                # Update items with received quantities
                for item in data.items:
                    if item.quantity_received is not None:
                        await conn.execute("""
                            UPDATE tenant_purchase_items
                            SET
                                quantity_received = $1,
                                item_condition = $2,
                                received_at = NOW()
                            WHERE purchase_id = $3 AND ingredient_id = $4
                        """, item.quantity_received, item.item_condition,
                        purchase_id, item.ingredient_id)

                # Create history entry
                await create_status_history_entry(
                    conn, purchase_id, tenant_id,
                    purchase['status'], target_status, user_id,
                    {
                        "package_condition": data.package_condition,
                        "partial_reception": data.partial
                    },
                    data.reception_notes
                )

                # Send email notification to supplier
                try:
                    purchase_info = await conn.fetchrow("""
                        SELECT tp.purchase_number, ts.name as supplier_name, ts.email as supplier_email,
                               ts.access_token as supplier_token, tsi.site as tenant_site
                        FROM tenant_purchases tp
                        JOIN tenant_suppliers ts ON tp.supplier_id = ts.id
                        LEFT JOIN tenant_sites tsi ON tp.tenant_id = tsi.tenant_id AND tsi.is_active = true
                        WHERE tp.id = $1
                        LIMIT 1
                    """, purchase_id)

                    if purchase_info and purchase_info['supplier_email']:
                        await send_purchase_status_notification(
                            supplier_email=purchase_info['supplier_email'],
                            supplier_name=purchase_info['supplier_name'],
                            purchase_number=purchase_info['purchase_number'],
                            status='received',
                            notes=data.reception_notes,
                            metadata={
                                "package_condition": data.package_condition,
                                "partial_reception": data.partial
                            },
                            supplier_token=str(purchase_info['supplier_token']) if purchase_info['supplier_token'] else None,
                            tenant_site=purchase_info['tenant_site']
                        )
                except Exception as email_error:
                    print(f"Error sending received email: {str(email_error)}")

                return {"success": True, "message": f"Purchase {target_status}"}

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error receiving purchase: {e}")
        raise HTTPException(status_code=500, detail="Error interno del servidor")

async def transition_to_verified(
    request: Request,
    response: Response,
    purchase_id: UUID,
    data: VerifyPurchaseData
) -> Dict[str, Any]:
    """Transition purchase to verified state"""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                # Get current purchase
                purchase = await conn.fetchrow("""
                    SELECT id, status FROM tenant_purchases
                    WHERE id = $1 AND tenant_id = $2
                """, purchase_id, tenant_id)

                if not purchase:
                    raise HTTPException(status_code=404, detail="Purchase not found")

                # Validate transition
                if not validate_state_transition(purchase['status'], 'verified'):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Cannot transition from {purchase['status']} to verified"
                    )

                # Update purchase
                await conn.execute("""
                    UPDATE tenant_purchases
                    SET
                        status = 'verified',
                        verified_by = $1,
                        verified_at = NOW(),
                        updated_at = NOW()
                    WHERE id = $2
                """, user_id, purchase_id)

                # Update items with quality assessment
                for item in data.items:
                    if item.quality_status is not None:
                        await conn.execute("""
                            UPDATE tenant_purchase_items
                            SET
                                quality_status = $1,
                                quality_notes = $2,
                                verification_notes = $3,
                                verified_at = NOW()
                            WHERE purchase_id = $4 AND ingredient_id = $5
                        """, item.quality_status, item.quality_notes,
                        item.verification_notes, purchase_id, item.ingredient_id)

                # Create history entry
                await create_status_history_entry(
                    conn, purchase_id, tenant_id,
                    purchase['status'], 'verified', user_id,
                    {"all_items_approved": data.all_items_approved},
                    data.verification_notes
                )

                # Send email notification to supplier
                try:
                    purchase_info = await conn.fetchrow("""
                        SELECT tp.purchase_number, ts.name as supplier_name, ts.email as supplier_email,
                               ts.access_token as supplier_token, tsi.site as tenant_site
                        FROM tenant_purchases tp
                        JOIN tenant_suppliers ts ON tp.supplier_id = ts.id
                        LEFT JOIN tenant_sites tsi ON tp.tenant_id = tsi.tenant_id AND tsi.is_active = true
                        WHERE tp.id = $1
                        LIMIT 1
                    """, purchase_id)

                    if purchase_info and purchase_info['supplier_email']:
                        await send_purchase_status_notification(
                            supplier_email=purchase_info['supplier_email'],
                            supplier_name=purchase_info['supplier_name'],
                            purchase_number=purchase_info['purchase_number'],
                            status='verified',
                            notes=data.verification_notes,
                            metadata={
                                "all_items_approved": data.all_items_approved
                            },
                            supplier_token=str(purchase_info['supplier_token']) if purchase_info['supplier_token'] else None,
                            tenant_site=purchase_info['tenant_site']
                        )
                except Exception as email_error:
                    print(f"Error sending verified email: {str(email_error)}")

                return {"success": True, "message": "Purchase verified successfully"}

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error verifying purchase: {e}")
        raise HTTPException(status_code=500, detail="Error interno del servidor")

async def transition_to_invoiced(
    request: Request,
    response: Response,
    purchase_id: UUID,
    data: InvoicePurchaseData
) -> Dict[str, Any]:
    """Transition purchase to invoiced state"""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                # Get current purchase
                purchase = await conn.fetchrow("""
                    SELECT id, status FROM tenant_purchases
                    WHERE id = $1 AND tenant_id = $2
                """, purchase_id, tenant_id)

                if not purchase:
                    raise HTTPException(status_code=404, detail="Purchase not found")

                # Validate transition
                if not validate_state_transition(purchase['status'], 'invoiced'):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Cannot transition from {purchase['status']} to invoiced"
                    )

                # Update purchase
                await conn.execute("""
                    UPDATE tenant_purchases
                    SET
                        status = 'invoiced',
                        invoice_number = $1,
                        total_amount = $2,
                        tax_amount = $3,
                        invoiced_at = NOW(),
                        updated_at = NOW()
                    WHERE id = $4
                """, data.invoice_number, data.invoice_amount, data.tax_amount, purchase_id)

                # Create history entry
                await create_status_history_entry(
                    conn, purchase_id, tenant_id,
                    purchase['status'], 'invoiced', user_id,
                    {
                        "invoice_number": data.invoice_number,
                        "invoice_amount": str(data.invoice_amount),
                        "payment_due_date": data.payment_due_date.isoformat() if data.payment_due_date else None
                    },
                    data.notes
                )

                # Send email notification to supplier
                try:
                    purchase_info = await conn.fetchrow("""
                        SELECT tp.purchase_number, ts.name as supplier_name, ts.email as supplier_email,
                               ts.access_token as supplier_token, tsi.site as tenant_site
                        FROM tenant_purchases tp
                        JOIN tenant_suppliers ts ON tp.supplier_id = ts.id
                        LEFT JOIN tenant_sites tsi ON tp.tenant_id = tsi.tenant_id AND tsi.is_active = true
                        WHERE tp.id = $1
                        LIMIT 1
                    """, purchase_id)

                    if purchase_info and purchase_info['supplier_email']:
                        await send_purchase_status_notification(
                            supplier_email=purchase_info['supplier_email'],
                            supplier_name=purchase_info['supplier_name'],
                            purchase_number=purchase_info['purchase_number'],
                            status='invoiced',
                            notes=data.notes,
                            metadata={
                                "invoice_number": data.invoice_number,
                                "invoice_total": float(data.invoice_amount),
                                "invoice_date": datetime.now().strftime('%d de %B de %Y'),
                                "payment_due_date": data.payment_due_date.strftime('%d de %B de %Y') if data.payment_due_date else None
                            },
                            supplier_token=str(purchase_info['supplier_token']) if purchase_info['supplier_token'] else None,
                            tenant_site=purchase_info['tenant_site']
                        )
                except Exception as email_error:
                    print(f"Error sending invoiced email: {str(email_error)}")

                return {"success": True, "message": "Invoice registered successfully"}

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error invoicing purchase: {e}")
        raise HTTPException(status_code=500, detail="Error interno del servidor")

async def transition_to_paid(
    request: Request,
    response: Response,
    purchase_id: UUID,
    data: PayPurchaseData
) -> Dict[str, Any]:
    """Transition purchase to paid state"""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                # Get current purchase
                purchase = await conn.fetchrow("""
                    SELECT id, status FROM tenant_purchases
                    WHERE id = $1 AND tenant_id = $2
                """, purchase_id, tenant_id)

                if not purchase:
                    raise HTTPException(status_code=404, detail="Purchase not found")

                # Validate transition
                if not validate_state_transition(purchase['status'], 'paid'):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Cannot transition from {purchase['status']} to paid"
                    )

                # Update purchase
                await conn.execute("""
                    UPDATE tenant_purchases
                    SET
                        status = 'paid',
                        payment_method = $1,
                        payment_reference = $2,
                        paid_at = NOW(),
                        updated_at = NOW()
                    WHERE id = $3
                """, data.payment_method, data.payment_reference, purchase_id)

                # Create history entry
                await create_status_history_entry(
                    conn, purchase_id, tenant_id,
                    purchase['status'], 'paid', user_id,
                    {
                        "payment_method": data.payment_method,
                        "payment_amount": str(data.payment_amount),
                        "payment_date": data.payment_date.isoformat()
                    },
                    data.notes
                )

                # Send email notification to supplier
                try:
                    purchase_info = await conn.fetchrow("""
                        SELECT tp.purchase_number, ts.name as supplier_name, ts.email as supplier_email,
                               ts.access_token as supplier_token, tsi.site as tenant_site
                        FROM tenant_purchases tp
                        JOIN tenant_suppliers ts ON tp.supplier_id = ts.id
                        LEFT JOIN tenant_sites tsi ON tp.tenant_id = tsi.tenant_id AND tsi.is_active = true
                        WHERE tp.id = $1
                        LIMIT 1
                    """, purchase_id)

                    if purchase_info and purchase_info['supplier_email']:
                        await send_purchase_status_notification(
                            supplier_email=purchase_info['supplier_email'],
                            supplier_name=purchase_info['supplier_name'],
                            purchase_number=purchase_info['purchase_number'],
                            status='paid',
                            notes=data.notes,
                            metadata={
                                "payment_method": data.payment_method,
                                "payment_reference": data.payment_reference,
                                "payment_date": data.payment_date.strftime('%d de %B de %Y')
                            },
                            supplier_token=str(purchase_info['supplier_token']) if purchase_info['supplier_token'] else None,
                            tenant_site=purchase_info['tenant_site']
                        )
                except Exception as email_error:
                    print(f"Error sending paid email: {str(email_error)}")

                return {"success": True, "message": "Payment registered successfully"}

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error recording payment: {e}")
        raise HTTPException(status_code=500, detail="Error interno del servidor")

async def cancel_purchase(
    request: Request,
    response: Response,
    purchase_id: UUID,
    data: CancelPurchaseData
) -> Dict[str, Any]:
    """Cancel a purchase order"""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                # Get current purchase
                purchase = await conn.fetchrow("""
                    SELECT id, status FROM tenant_purchases
                    WHERE id = $1 AND tenant_id = $2
                """, purchase_id, tenant_id)

                if not purchase:
                    raise HTTPException(status_code=404, detail="Purchase not found")

                # Check if already in final state
                if purchase['status'] in ['paid', 'cancelled']:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Cannot cancel purchase in {purchase['status']} state"
                    )

                # Update purchase
                await conn.execute("""
                    UPDATE tenant_purchases
                    SET
                        status = 'cancelled',
                        cancellation_reason = $1,
                        cancelled_at = NOW(),
                        updated_at = NOW()
                    WHERE id = $2
                """, data.cancellation_reason, purchase_id)

                # Create history entry
                await create_status_history_entry(
                    conn, purchase_id, tenant_id,
                    purchase['status'], 'cancelled', user_id,
                    {"cancellation_reason": data.cancellation_reason},
                    data.notes
                )

                return {"success": True, "message": "Purchase cancelled successfully"}

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error cancelling purchase: {e}")
        raise HTTPException(status_code=500, detail="Error interno del servidor")

# =============================================================================
# QUOTATION COMPLETION
# =============================================================================

async def complete_quotation(
    request: Request,
    response: Response,
    purchase_id: UUID,
    data: dict
) -> Dict[str, Any]:
    """Complete a quotation by adding prices and transitioning to pending"""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                # Get current purchase
                purchase = await conn.fetchrow("""
                    SELECT id, status FROM tenant_purchases
                    WHERE id = $1 AND tenant_id = $2
                """, purchase_id, tenant_id)

                if not purchase:
                    raise HTTPException(status_code=404, detail="Purchase not found")

                # Validate state transition
                if not validate_state_transition(purchase['status'], 'pending'):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Cannot complete quotation from '{purchase['status']}' status"
                    )

                # Update purchase items with prices
                for item in data.get('items', []):
                    await conn.execute("""
                        UPDATE tenant_purchase_items
                        SET
                            unit_cost = $1,
                            total_cost = $2,
                            notes = COALESCE($3, notes)
                        WHERE id = $4 AND purchase_id = $5
                    """, item['unit_cost'], item['total_cost'],
                    item.get('notes'), item['id'], purchase_id)

                # Update purchase totals and status
                await conn.execute("""
                    UPDATE tenant_purchases
                    SET
                        status = 'pending',
                        tax_amount = $1,
                        total_amount = $2,
                        notes = COALESCE($3, notes),
                        updated_at = NOW()
                    WHERE id = $4
                """, data.get('tax_amount', 0), data.get('total_amount', 0),
                data.get('notes'), purchase_id)

                # Create history entry
                await create_status_history_entry(
                    conn, purchase_id, tenant_id,
                    purchase['status'], 'pending', user_id,
                    {
                        "items_priced": len(data.get('items', [])),
                        "total_amount": str(data.get('total_amount', 0))
                    },
                    "Quotation completed with prices from supplier"
                )

                return {"success": True, "message": "Quotation completed successfully"}

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error completing quotation: {e}")
        raise HTTPException(status_code=500, detail="Error interno del servidor")

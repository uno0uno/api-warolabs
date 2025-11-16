"""
Purchase Tracking Service
Handles status transitions, attachments, and history for purchase orders
"""

from typing import Optional, List, Dict, Any
from uuid import UUID
from datetime import datetime
from fastapi import Request, Response, HTTPException, UploadFile
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError
from app.services.aws_s3_service import AWSS3Service
import logging

logger = logging.getLogger(__name__)
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
    'confirmed': ['preparing', 'paid', 'invoiced', 'cancelled'],  # Can pay before invoice (contado) or invoice first (credito)
    'preparing': ['paid', 'invoiced', 'cancelled'],  # Can pay before invoice (contado) or invoice first (credito)
    'paid': ['invoiced'],  # After payment, supplier can invoice (for contado flow)
    'invoiced': ['shipped'],  # Ship after invoicing
    'shipped': ['received', 'partially_received', 'overdue'],
    'partially_received': ['received', 'overdue'],
    'received': ['verified'],
    'verified': ['paid'],  # Pay after verification (traditional flow - credito)
    'cancelled': [],  # Final state
    'overdue': ['shipped', 'received', 'cancelled']  # Can resume flow
}

def validate_state_transition(from_status: str, to_status: str) -> bool:
    """Validate if a state transition is allowed"""
    allowed_transitions = STATE_TRANSITIONS.get(from_status, [])
    return to_status in allowed_transitions

# =============================================================================
# ATTACHMENT UPLOAD HELPER
# =============================================================================

async def upload_purchase_attachments(
    conn,
    tenant_id: UUID,
    purchase_id: UUID,
    user_id: UUID,
    files: List[UploadFile],
    attachment_type: str,
    description_prefix: str,
    log_prefix: str = "UPLOAD"
) -> None:
    """
    Helper function to upload purchase attachments to S3/R2 and save to database

    Args:
        conn: Database connection
        tenant_id: Tenant UUID
        purchase_id: Purchase UUID
        user_id: User UUID who is uploading
        files: List of UploadFile objects
        attachment_type: Type of attachment (e.g., 'shipping_label', 'invoice', 'payment_proof')
        description_prefix: Prefix for attachment description
        log_prefix: Prefix for log messages
    """
    if not files:
        return

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
                    tenant_id,
                    purchase_id,
                    s3_key,  # path (required)
                    file.filename,
                    file.size or 0,
                    file.content_type or 'application/octet-stream',
                    attachment_type,
                    description_prefix,
                    user_id,
                    s3_key,
                    file_url
                )
        except Exception:
            pass
pass

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

        raise HTTPException(status_code=500, detail="Error interno del servidor")

async def get_transition_detail(
    request: Request,
    response: Response,
    purchase_id: UUID,
    transition_id: UUID
):
    """Get detailed information about a specific transition including attachments"""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Verify purchase belongs to tenant and get purchase info with all details
            purchase = await conn.fetchrow("""
                SELECT
                    tp.id,
                    tp.purchase_number,
                    tp.purchase_date,
                    tp.payment_type,
                    tp.status,
                    ts.name as supplier_name
                FROM tenant_purchases tp
                LEFT JOIN tenant_suppliers ts ON tp.supplier_id = ts.id
                WHERE tp.id = $1 AND tp.tenant_id = $2
            """, purchase_id, tenant_id)

            if not purchase:
                raise HTTPException(status_code=404, detail="Purchase not found")

            # Get specific transition with user info
            transition_data = await conn.fetchrow("""
                SELECT
                    psh.id,
                    psh.purchase_id,
                    psh.tenant_id,
                    psh.from_status,
                    psh.to_status,
                    psh.changed_by,
                    psh.changed_at,
                    psh.metadata,
                    psh.notes,
                    psh.created_at,
                    p.name as user_name,
                    p.email as user_email
                FROM purchase_status_history psh
                LEFT JOIN profile p ON psh.changed_by = p.id
                WHERE psh.id = $1 AND psh.purchase_id = $2
            """, transition_id, purchase_id)

            if not transition_data:
                raise HTTPException(status_code=404, detail="Transition not found")

            # Get attachments uploaded around this transition (±5 minutes)
            attachments_data = await conn.fetch("""
                SELECT
                    id,
                    purchase_id,
                    s3_key,
                    file_name,
                    file_size,
                    mime_type,
                    attachment_type,
                    description,
                    uploaded_at,
                    created_at
                FROM purchase_attachments
                WHERE purchase_id = $1
                ORDER BY uploaded_at DESC
            """, purchase_id)

        # Generate presigned URLs for attachments
        s3_service = AWSS3Service()
        transition_time = transition_data['changed_at']
        related_attachments = []

        for att_row in attachments_data:
            # Filter by timestamp (±5 minutes)
            upload_time = att_row['created_at']
            time_diff = abs((upload_time - transition_time).total_seconds())

            if time_diff < 300:  # 5 minutes = 300 seconds
                att_dict = dict(att_row)
                if att_dict.get('s3_key'):
                    try:
                        presigned_url = await s3_service.get_presigned_url(
                            att_dict['s3_key'],
                            expiration=3600
                        )
                        att_dict['s3_url'] = presigned_url
                    except Exception as e:
                        print(f"Error generating presigned URL: {e}")
                        att_dict['s3_url'] = None
                else:
                    att_dict['s3_url'] = None
                related_attachments.append(att_dict)

        # Parse transition metadata
        import json
        transition_dict = dict(transition_data)

        # Extract user info
        user_name = transition_dict.pop('user_name', None)
        user_email = transition_dict.pop('user_email', None)

        if transition_dict.get('metadata') and isinstance(transition_dict['metadata'], str):
            try:
                transition_dict['metadata'] = json.loads(transition_dict['metadata'])
            except json.JSONDecodeError:
                transition_dict['metadata'] = {}

        # Add user info to transition
        transition_dict['user_name'] = user_name
        transition_dict['user_email'] = user_email

        # Build response
        return {
            "success": True,
            "data": {
                "transition": transition_dict,
                "purchase": {
                    "purchase_number": purchase['purchase_number'],
                    "purchase_date": purchase['purchase_date'].isoformat() if purchase['purchase_date'] else None,
                    "payment_type": purchase['payment_type'],
                    "status": purchase['status'],
                    "supplier_name": purchase['supplier_name']
                },
                "attachments": related_attachments
            }
        }

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting transition detail: {e}")
        raise HTTPException(status_code=500, detail="Error getting transition detail")


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
                    created_at,
                    s3_key
                FROM purchase_attachments
                WHERE purchase_id = $1
                ORDER BY uploaded_at DESC
            """, purchase_id)

            # Generate presigned URLs for each attachment
            s3_service = AWSS3Service()
            attachments = []
            for row in attachments_data:
                row_dict = dict(row)
                # Generate fresh presigned URL if s3_key exists
                if row_dict.get('s3_key'):
                    try:
                        presigned_url = await s3_service.get_presigned_url(
                            row_dict['s3_key'],
                            expiration=3600
                        )
                        row_dict['s3_url'] = presigned_url
                    except Exception as e:
                        print(f"Error generating presigned URL for attachment {row_dict['id']}: {e}")
                        row_dict['s3_url'] = None
                else:
                    row_dict['s3_url'] = None

                attachments.append(PurchaseAttachment(**row_dict))

            return AttachmentsResponse(data=attachments)

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:

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
                    pass

                return {"success": True, "message": "Purchase confirmed successfully"}

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:

        raise HTTPException(status_code=500, detail="Error interno del servidor")

async def transition_to_shipped(
    request: Request,
    response: Response,
    purchase_id: UUID,
    tracking_number: str,
    carrier: str,
    estimated_delivery_date: Optional[str] = None,
    package_count: Optional[int] = None,
    notes: Optional[str] = None,
    files: List[UploadFile] = []
) -> Dict[str, Any]:
    """Transition purchase to shipped state with optional file attachments"""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        # Parse estimated_delivery_date if provided
        from datetime import datetime
        estimated_delivery_dt = None
        if estimated_delivery_date:
            try:
                estimated_delivery_dt = datetime.fromisoformat(estimated_delivery_date.replace('Z', '+00:00'))
            except:
                pass

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
                """, tracking_number, carrier, estimated_delivery_dt,
                package_count, purchase_id)

                # Create history entry
                await create_status_history_entry(
                    conn, purchase_id, tenant_id,
                    purchase['status'], 'shipped', user_id,
                    {
                        "tracking_number": tracking_number,
                        "carrier": carrier,
                        "package_count": package_count
                    },
                    notes
                )

                # Upload attachments if provided
                await upload_purchase_attachments(
                    conn=conn,
                    tenant_id=tenant_id,
                    purchase_id=purchase_id,
                    user_id=user_id,
                    files=files,
                    attachment_type='shipping_label',
                    description_prefix=f'Envío: {tracking_number}',
                    log_prefix='SHIP-ADMIN'
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
                            notes=notes,
                            metadata={
                                "tracking_number": tracking_number,
                                "carrier": carrier,
                                "estimated_delivery_date": estimated_delivery_dt.strftime('%d de %B de %Y') if estimated_delivery_dt else None,
                                "package_count": package_count
                            },
                            supplier_token=str(purchase_info['supplier_token']) if purchase_info['supplier_token'] else None,
                            tenant_site=purchase_info['tenant_site']
                        )
                except Exception as email_error:
                    pass

                return {"success": True, "message": "Purchase marked as shipped"}

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:

        raise HTTPException(status_code=500, detail="Error interno del servidor")

async def transition_to_received(
    request: Request,
    response: Response,
    purchase_id: UUID,
    items_data: str,
    package_condition: str,
    reception_notes: Optional[str] = None,
    partial: bool = False,
    files: List[UploadFile] = []
) -> Dict[str, Any]:
    """Transition purchase to received state with optional file attachments"""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        # Parse items data from JSON string
        import json
        try:
            items = json.loads(items_data)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid items data format")

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
                target_status = 'partially_received' if partial else 'received'

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
                """, target_status, package_condition, user_id, purchase_id)

                # Update items with received quantities
                for item in items:
                    quantity_received = item.get('quantity_received')
                    if quantity_received is not None:
                        await conn.execute("""
                            UPDATE tenant_purchase_items
                            SET
                                quantity_received = $1,
                                item_condition = $2,
                                received_at = NOW()
                            WHERE purchase_id = $3 AND ingredient_id = $4
                        """, quantity_received, item.get('item_condition'),
                        purchase_id, item.get('ingredient_id'))

                # Create history entry
                await create_status_history_entry(
                    conn, purchase_id, tenant_id,
                    purchase['status'], target_status, user_id,
                    {
                        "package_condition": package_condition,
                        "partial_reception": partial
                    },
                    reception_notes
                )

                # Upload attachments if provided
                await upload_purchase_attachments(
                    conn=conn,
                    tenant_id=tenant_id,
                    purchase_id=purchase_id,
                    user_id=user_id,
                    files=files,
                    attachment_type='delivery_photo',
                    description_prefix='Recepción de mercancía',
                    log_prefix='RECEIVE'
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
                            notes=reception_notes,
                            metadata={
                                "package_condition": package_condition,
                                "partial_reception": partial
                            },
                            supplier_token=str(purchase_info['supplier_token']) if purchase_info['supplier_token'] else None,
                            tenant_site=purchase_info['tenant_site']
                        )
                except Exception as email_error:
                    pass

                return {"success": True, "message": f"Purchase {target_status}"}

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:

        raise HTTPException(status_code=500, detail="Error interno del servidor")

async def transition_to_verified(
    request: Request,
    response: Response,
    purchase_id: UUID,
    items_data: str,
    all_items_approved: bool,
    verification_notes: Optional[str] = None,
    files: List[UploadFile] = []
) -> Dict[str, Any]:
    """Transition purchase to verified state with optional file attachments"""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        # Parse items data from JSON string
        import json
        try:
            items = json.loads(items_data)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid items data format")

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
                for item in items:
                    quality_status = item.get('quality_status')
                    if quality_status is not None:
                        await conn.execute("""
                            UPDATE tenant_purchase_items
                            SET
                                quality_status = $1,
                                quality_notes = $2,
                                verification_notes = $3,
                                verified_at = NOW()
                            WHERE purchase_id = $4 AND ingredient_id = $5
                        """, quality_status, item.get('quality_notes'),
                        item.get('verification_notes'), purchase_id, item.get('ingredient_id'))

                # Create history entry
                await create_status_history_entry(
                    conn, purchase_id, tenant_id,
                    purchase['status'], 'verified', user_id,
                    {"all_items_approved": all_items_approved},
                    verification_notes
                )

                # Upload attachments if provided
                await upload_purchase_attachments(
                    conn=conn,
                    tenant_id=tenant_id,
                    purchase_id=purchase_id,
                    user_id=user_id,
                    files=files,
                    attachment_type='quality_photo',
                    description_prefix='Verificación de calidad',
                    log_prefix='VERIFY'
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
                            notes=verification_notes,
                            metadata={
                                "all_items_approved": all_items_approved
                            },
                            supplier_token=str(purchase_info['supplier_token']) if purchase_info['supplier_token'] else None,
                            tenant_site=purchase_info['tenant_site']
                        )
                except Exception as email_error:
                    pass

                return {"success": True, "message": "Purchase verified successfully"}

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:

        raise HTTPException(status_code=500, detail="Error interno del servidor")

async def transition_to_invoiced(
    request: Request,
    response: Response,
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
    """Transition purchase to invoiced state with optional file attachments"""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        # Parse dates
        from datetime import datetime, timedelta
        try:
            invoice_dt = datetime.fromisoformat(invoice_date.replace('Z', '+00:00'))
        except:
            invoice_dt = datetime.now()

        payment_due_dt = None
        if payment_due_date:
            try:
                payment_due_dt = datetime.fromisoformat(payment_due_date.replace('Z', '+00:00'))
            except:
                pass

        async with get_db_connection() as conn:
            async with conn.transaction():
                # Get current purchase with payment info
                purchase = await conn.fetchrow("""
                    SELECT id, status, payment_type, credit_days, payment_balance
                    FROM tenant_purchases
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

                # Calculate payment_due_date if credit_days is set
                if purchase['credit_days'] and purchase['credit_days'] > 0:
                    payment_due_dt = invoice_dt + timedelta(days=purchase['credit_days'])
                elif not payment_due_dt and credit_days:
                    # Use provided credit_days if no purchase credit_days
                    payment_due_dt = invoice_dt + timedelta(days=credit_days)

                # Set payment_balance to invoice_amount for tracking partial payments
                payment_balance = invoice_amount if invoice_amount else 0

                # Update purchase with invoice and payment info
                await conn.execute("""
                    UPDATE tenant_purchases
                    SET
                        status = 'invoiced',
                        invoice_number = $1,
                        invoice_date = $2,
                        invoice_amount = $3,
                        total_amount = $4,
                        tax_amount = $5,
                        payment_due_date = $6,
                        payment_balance = $7,
                        invoiced_at = NOW(),
                        updated_at = NOW()
                    WHERE id = $8
                """,
                    invoice_number,
                    invoice_dt,
                    invoice_amount,
                    invoice_amount,  # total_amount = invoice_amount
                    tax_amount,
                    payment_due_dt,
                    payment_balance,
                    purchase_id
                )

                # Create history entry
                await create_status_history_entry(
                    conn, purchase_id, tenant_id,
                    purchase['status'], 'invoiced', user_id,
                    {
                        "invoice_number": invoice_number,
                        "invoice_amount": str(invoice_amount) if invoice_amount else None,
                        "payment_due_date": payment_due_dt.isoformat() if payment_due_dt else None
                    },
                    notes
                )

                # Upload attachments if provided
                await upload_purchase_attachments(
                    conn=conn,
                    tenant_id=tenant_id,
                    purchase_id=purchase_id,
                    user_id=user_id,
                    files=files,
                    attachment_type='invoice',
                    description_prefix=f'Factura: {invoice_number}',
                    log_prefix='INVOICE-ADMIN'
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
                            notes=notes,
                            metadata={
                                "invoice_number": invoice_number,
                                "invoice_total": float(invoice_amount) if invoice_amount else 0,
                                "invoice_date": invoice_dt.strftime('%d de %B de %Y'),
                                "payment_due_date": payment_due_dt.strftime('%d de %B de %Y') if payment_due_dt else None
                            },
                            supplier_token=str(purchase_info['supplier_token']) if purchase_info['supplier_token'] else None,
                            tenant_site=purchase_info['tenant_site']
                        )
                except Exception as email_error:
                    pass

                return {"success": True, "message": "Invoice registered successfully"}

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:

        raise HTTPException(status_code=500, detail="Error interno del servidor")

async def transition_to_paid(
    request: Request,
    response: Response,
    purchase_id: UUID,
    payment_method: str,
    payment_reference: str,
    payment_amount: float,
    payment_date: str,
    notes: Optional[str] = None,
    files: List[UploadFile] = []
) -> Dict[str, Any]:
    """Transition purchase to paid state with optional file attachments"""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        # Parse payment_date
        from datetime import datetime
        try:
            payment_dt = datetime.fromisoformat(payment_date.replace('Z', '+00:00'))
        except:
            payment_dt = datetime.now()

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
                        payment_amount = $3,
                        payment_date = $4,
                        paid_at = NOW(),
                        updated_at = NOW()
                    WHERE id = $5
                """, payment_method, payment_reference, payment_amount, payment_dt, purchase_id)

                # Create history entry
                await create_status_history_entry(
                    conn, purchase_id, tenant_id,
                    purchase['status'], 'paid', user_id,
                    {
                        "payment_method": payment_method,
                        "payment_amount": str(payment_amount),
                        "payment_date": payment_dt.isoformat()
                    },
                    notes
                )

                # Upload attachments if provided
                await upload_purchase_attachments(
                    conn=conn,
                    tenant_id=tenant_id,
                    purchase_id=purchase_id,
                    user_id=user_id,
                    files=files,
                    attachment_type='payment_proof',
                    description_prefix=f'Comprobante de pago: {payment_reference}',
                    log_prefix='PAY'
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
                            notes=notes,
                            metadata={
                                "payment_method": payment_method,
                                "payment_reference": payment_reference,
                                "payment_date": payment_dt.strftime('%d de %B de %Y')
                            },
                            supplier_token=str(purchase_info['supplier_token']) if purchase_info['supplier_token'] else None,
                            tenant_site=purchase_info['tenant_site']
                        )
                except Exception as email_error:
                    pass

                return {"success": True, "message": "Payment registered successfully"}

    except AuthenticationError:
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in transition_to_paid: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error registrando pago: {str(e)}")

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

        raise HTTPException(status_code=500, detail="Error interno del servidor")

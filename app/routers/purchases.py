from fastapi import APIRouter, Request, Response, Query
from uuid import UUID
from typing import Optional
from app.services.purchases_service import (
    get_purchases_list,
    get_purchase_by_id,
    create_purchase,
    update_purchase,
    delete_purchase
)
from app.services.purchase_tracking_service import (
    # State transitions
    transition_to_confirmed,
    transition_to_shipped,
    transition_to_received,
    transition_to_verified,
    transition_to_invoiced,
    transition_to_paid,
    cancel_purchase,
    complete_quotation,
    # History and attachments
    get_purchase_status_history,
    get_purchase_attachments,
    create_purchase_attachment
)
from app.models.purchase import (
    Purchase,
    PurchaseCreate,
    PurchaseUpdate,
    PurchaseResponse,
    PurchasesListResponse,
    # State transition models
    ConfirmPurchaseData,
    ShipPurchaseData,
    ReceivePurchaseData,
    VerifyPurchaseData,
    InvoicePurchaseData,
    PayPurchaseData,
    CancelPurchaseData,
    # History and attachment models
    StatusHistoryResponse,
    AttachmentsResponse,
    PurchaseAttachmentCreate
)

router = APIRouter()

@router.get("/next-number")
async def get_next_purchase_number(
    request: Request,
    response: Response
):
    """
    Get the next auto-generated purchase number
    Preview only - actual number is generated on creation
    """
    from app.core.middleware import require_valid_session
    from app.database import get_db_connection
    from datetime import datetime

    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id

    if not tenant_id:
        return {"next_number": "WR-2025-0001"}

    async with get_db_connection() as conn:
        current_year = datetime.now().year

        last_purchase = await conn.fetchrow("""
            SELECT purchase_number
            FROM tenant_purchases
            WHERE tenant_id = $1
                AND purchase_number LIKE $2
            ORDER BY created_at DESC
            LIMIT 1
        """, tenant_id, f'WR-{current_year}-%')

        if last_purchase and last_purchase['purchase_number']:
            last_number = int(last_purchase['purchase_number'].split('-')[-1])
            next_number = last_number + 1
        else:
            next_number = 1

        return {
            "next_number": f"WR-{current_year}-{next_number:04d}"
        }

@router.get("", response_model=PurchasesListResponse)
async def get_purchases_endpoint(
    request: Request,
    response: Response,
    page: int = Query(default=1, ge=1, description="Page number"),
    limit: int = Query(default=50, ge=1, le=250, description="Items per page"),
    search: Optional[str] = Query(default=None, description="Search by purchase number or invoice number"),
    status: Optional[str] = Query(default=None, description="Filter by status"),
    supplier_id: Optional[UUID] = Query(default=None, description="Filter by supplier ID")
):
    """
    Get purchases list with tenant isolation
    Requires valid session with tenant context
    """
    return await get_purchases_list(
        request, response, page, limit, search, status, supplier_id
    )

@router.get("/{purchase_id}", response_model=PurchaseResponse)
async def get_purchase_endpoint(
    purchase_id: UUID,
    request: Request,
    response: Response
):
    """
    Get a specific purchase by ID with tenant isolation
    """
    return await get_purchase_by_id(request, response, purchase_id)

@router.post("", response_model=PurchaseResponse)
async def create_purchase_endpoint(
    purchase_data: PurchaseCreate,
    request: Request,
    response: Response
):
    """
    Create a new purchase with tenant isolation
    """
    return await create_purchase(request, response, purchase_data)

@router.put("/{purchase_id}", response_model=PurchaseResponse)
async def update_purchase_endpoint(
    purchase_id: UUID,
    purchase_data: PurchaseUpdate,
    request: Request,
    response: Response
):
    """
    Update an existing purchase with tenant isolation
    """
    return await update_purchase(request, response, purchase_id, purchase_data)

@router.delete("/{purchase_id}")
async def delete_purchase_endpoint(
    purchase_id: UUID,
    request: Request,
    response: Response
):
    """
    Delete a purchase with tenant isolation
    """
    return await delete_purchase(request, response, purchase_id)

# =============================================================================
# STATE TRANSITION ENDPOINTS
# =============================================================================

@router.post("/{purchase_id}/confirm")
async def confirm_purchase_endpoint(
    purchase_id: UUID,
    data: ConfirmPurchaseData,
    request: Request,
    response: Response
):
    """
    Transition purchase to CONFIRMED state
    Records supplier confirmation number and estimated delivery date
    """
    return await transition_to_confirmed(request, response, purchase_id, data)

@router.post("/{purchase_id}/ship")
async def ship_purchase_endpoint(
    purchase_id: UUID,
    data: ShipPurchaseData,
    request: Request,
    response: Response
):
    """
    Transition purchase to SHIPPED state
    Records tracking number, carrier, and package information
    """
    return await transition_to_shipped(request, response, purchase_id, data)

@router.post("/{purchase_id}/receive")
async def receive_purchase_endpoint(
    purchase_id: UUID,
    data: ReceivePurchaseData,
    request: Request,
    response: Response
):
    """
    Transition purchase to RECEIVED or PARTIALLY_RECEIVED state
    Records quantities received and package condition
    """
    return await transition_to_received(request, response, purchase_id, data)

@router.post("/{purchase_id}/verify")
async def verify_purchase_endpoint(
    purchase_id: UUID,
    data: VerifyPurchaseData,
    request: Request,
    response: Response
):
    """
    Transition purchase to VERIFIED state
    Records quality assessment and verification notes
    """
    return await transition_to_verified(request, response, purchase_id, data)

@router.post("/{purchase_id}/invoice")
async def invoice_purchase_endpoint(
    purchase_id: UUID,
    data: InvoicePurchaseData,
    request: Request,
    response: Response
):
    """
    Transition purchase to INVOICED state
    Records invoice details and payment due date
    """
    return await transition_to_invoiced(request, response, purchase_id, data)

@router.post("/{purchase_id}/pay")
async def pay_purchase_endpoint(
    purchase_id: UUID,
    data: PayPurchaseData,
    request: Request,
    response: Response
):
    """
    Transition purchase to PAID state
    Records payment method and reference
    """
    return await transition_to_paid(request, response, purchase_id, data)

@router.post("/{purchase_id}/cancel")
async def cancel_purchase_endpoint(
    purchase_id: UUID,
    data: CancelPurchaseData,
    request: Request,
    response: Response
):
    """
    Cancel a purchase order
    Can be done from any state except PAID or CANCELLED
    """
    return await cancel_purchase(request, response, purchase_id, data)

# =============================================================================
# STATUS HISTORY AND ATTACHMENTS
# =============================================================================

@router.get("/{purchase_id}/history", response_model=StatusHistoryResponse)
async def get_purchase_history_endpoint(
    purchase_id: UUID,
    request: Request,
    response: Response
):
    """
    Get complete status history for a purchase
    Returns all state transitions with timestamps and metadata
    """
    return await get_purchase_status_history(request, response, purchase_id)

@router.get("/{purchase_id}/attachments", response_model=AttachmentsResponse)
async def get_purchase_attachments_endpoint(
    purchase_id: UUID,
    request: Request,
    response: Response
):
    """
    Get all attachments for a purchase
    Includes invoices, shipping labels, quality photos, etc.
    """
    return await get_purchase_attachments(request, response, purchase_id)

@router.post("/{purchase_id}/attachments")
async def create_purchase_attachment_endpoint(
    purchase_id: UUID,
    attachment_data: PurchaseAttachmentCreate,
    request: Request,
    response: Response
):
    """
    Upload an attachment for a purchase
    Stores reference to Cloudflare R2 file
    """
    # Ensure purchase_id in data matches URL parameter
    attachment_data.purchase_id = purchase_id
    return await create_purchase_attachment(request, response, attachment_data)

@router.post("/{purchase_id}/complete-quotation")
async def complete_quotation_endpoint(
    purchase_id: UUID,
    data: dict,
    request: Request,
    response: Response
):
    """
    Complete a quotation by adding prices
    Transitions from quotation to pending status
    """
    return await complete_quotation(request, response, purchase_id, data)

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response, Query, Form, File, UploadFile
from uuid import UUID
from typing import Optional, List
from app.core.permissions import Module, require_module
from app.services.purchases_service import (
    get_purchases_list,
    get_purchase_by_id,
    create_purchase,
    update_purchase,
    extract_invoice_data
)
from app.services.purchase_tracking_service import (
    # State transitions
    transition_to_confirmed,
    transition_to_shipped,
    transition_to_received,
    transition_to_invoiced,
    transition_to_paid,
    cancel_purchase,
    complete_quotation,
    # History and attachments
    get_purchase_status_history,
    get_transition_detail,
    get_purchase_attachments,
    create_purchase_attachment
)
from app.services.direct_purchase_service import (
    create_direct_purchase,
    get_direct_purchases_list,
    get_direct_purchase_by_id,
    get_supplier_catalog_prices,
    update_direct_purchase,
    upload_direct_purchase_attachments
)
from app.services.analytics_service import run_anomaly_checks_for_purchase
from app.core.middleware import require_valid_session
from app.models.purchase import (
    PurchaseCreate,
    PurchaseUpdate,
    PurchaseResponse,
    PurchasesListResponse,
    # State transition models
    ConfirmPurchaseData,
    CancelPurchaseData,
    # History and attachment models
    StatusHistoryResponse,
    AttachmentsResponse,
    PurchaseAttachmentCreate,
    DirectPurchaseCreate,
    DirectPurchaseUpdate
)

router = APIRouter()


@router.post("/extract-invoice", dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def extract_invoice(
    request: Request,
    file: UploadFile = File(...)
):
    """
    Extract structured data from invoice image using Google Gemini 1.5 Flash.
    Receives an image file, returns structured JSON with invoice fields.
    Enforces per-tenant scan quota (1 000 scans/period). Returns 429 when exceeded.
    """
    return await extract_invoice_data(request, file)


@router.get("/scan-usage", dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def get_scan_usage_endpoint(request: Request):
    """
    Returns current period scan quota usage for the authenticated tenant.

    Response:
    {
      "scans_used": 342,
      "scans_limit": 1000,
      "period_start": "2026-03-01",
      "period_end": "2026-04-01",
      "percentage": 34.2
    }
    """
    from app.database import get_db_connection
    from app.services.billing_service import get_scan_usage

    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id

    if not tenant_id:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Tenant ID required")

    async with get_db_connection(use_transaction=False) as conn:
        return await get_scan_usage(tenant_id, conn)


@router.get("/next-number", dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
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

# =============================================================================
# DIRECT PURCHASES ENDPOINTS (Compras Directas - WR-CD-XXXX)
# =============================================================================

@router.get("/direct", dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def get_direct_purchases_endpoint(
    request: Request,
    response: Response,
    page: int = Query(default=1, ge=1, description="Page number"),
    limit: int = Query(default=50, ge=1, le=250, description="Items per page"),
    search: Optional[str] = Query(default=None, description="Search by purchase number, invoice number, or supplier name"),
    status: Optional[str] = Query(default=None, description="Filter by status"),
    supplier_id: Optional[UUID] = Query(default=None, description="Filter by supplier ID"),
    date_filter: Optional[str] = Query(default=None, description="Filter by date range (today, yesterday, last_week, 15_days, 1_month, 3_months)")
):
    """
    Get list of direct purchases (Compras Directas) with tenant isolation.
    These are purchases created via the simplified flow with immediate stock update.
    """
    return await get_direct_purchases_list(
        request=request,
        response=response,
        page=page,
        limit=limit,
        search=search,
        status=status,
        supplier_id=supplier_id,
        date_filter=date_filter
    )


@router.post("/direct", dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def create_direct_purchase_endpoint(
    purchase_data: DirectPurchaseCreate,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks
):
    """
    Create a direct purchase (Compra Directa) with immediate inventory update.

    This endpoint:
    1. Creates a purchase with status 'received' and is_direct_entry=True
    2. Updates inventory immediately for all items
    3. Optionally attaches invoice and payment documents
    4. Generates purchase number with WR-CD-XXXX format
    5. Schedules anomaly detection in background (non-blocking)
    """
    result = await create_direct_purchase(
        request=request,
        response=response,
        supplier_id=purchase_data.supplier_id,
        items_data=purchase_data.items_data,
        new_units_data=purchase_data.new_units_data,
        payment_type=purchase_data.payment_type,
        payment_terms=purchase_data.payment_terms,
        notes=purchase_data.notes,
        invoice_number=purchase_data.invoice_number,
        invoice_amount=purchase_data.invoice_amount,
        invoice_date=purchase_data.invoice_date,
        payment_method=purchase_data.payment_method,
        payment_method_id=purchase_data.payment_method_id,
        payment_reference=purchase_data.payment_reference,
        payment_amount=purchase_data.payment_amount,
        payment_date=purchase_data.payment_date,
        purchase_date=purchase_data.purchase_date
    )

    # Schedule anomaly check after the purchase transaction has committed
    try:
        if result.get("success") and result.get("data", {}).get("id"):
            session_context = require_valid_session(request)
            tenant_id = session_context.tenant_id
            purchase_id = UUID(result["data"]["id"])
            background_tasks.add_task(run_anomaly_checks_for_purchase, purchase_id, tenant_id)
    except Exception:
        pass  # Never fail the purchase response due to background scheduling

    return result


@router.get("/direct/next-number", dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def get_next_direct_purchase_number(
    request: Request,
    response: Response
):
    """
    Get the next auto-generated direct purchase number (WR-CD-XXXX format)
    Preview only - actual number is generated on creation
    """
    from app.core.middleware import require_valid_session
    from app.database import get_db_connection
    from datetime import datetime

    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id

    if not tenant_id:
        return {"next_number": "WR-CD-2025-0001"}

    async with get_db_connection() as conn:
        current_year = datetime.now().year
        prefix = f'WR-CD-{current_year}-'

        last_purchase = await conn.fetchrow("""
            SELECT purchase_number
            FROM tenant_purchases
            WHERE tenant_id = $1
                AND purchase_number LIKE $2
            ORDER BY purchase_number DESC
            LIMIT 1
        """, tenant_id, f'{prefix}%')

        if last_purchase and last_purchase['purchase_number']:
            try:
                last_number = int(last_purchase['purchase_number'].split('-')[-1])
                next_number = last_number + 1
            except (ValueError, IndexError):
                next_number = 1
        else:
            next_number = 1

        return {
            "next_number": f"{prefix}{next_number:04d}"
        }


@router.get("/direct/{purchase_id}", dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def get_direct_purchase_endpoint(
    purchase_id: UUID,
    request: Request,
    response: Response
):
    """
    Get a specific direct purchase by ID with all details (items, history, attachments)
    """
    return await get_direct_purchase_by_id(
        request=request,
        response=response,
        purchase_id=purchase_id
    )


@router.put("/direct/{purchase_id}", dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def update_direct_purchase_endpoint(
    purchase_id: UUID,
    purchase_data: DirectPurchaseUpdate,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
):
    """
    Update a direct purchase (Compra Directa) - JSON payload
    Updates purchase items and metadata. Use separate endpoints for file uploads.
    """
    result = await update_direct_purchase(
        request=request,
        response=response,
        purchase_id=purchase_id,
        items_data=purchase_data.items_data,
        purchase_date=purchase_data.purchase_date,
        notes=purchase_data.notes,
        invoice_number=purchase_data.invoice_number,
        payment_type=purchase_data.payment_type,
        payment_method=purchase_data.payment_method,
        payment_method_id=purchase_data.payment_method_id,
        payment_reference=purchase_data.payment_reference,
        payment_amount=purchase_data.payment_amount,
        payment_date=purchase_data.payment_date
    )

    try:
        if result.get("success"):
            session_context = require_valid_session(request)
            tenant_id = session_context.tenant_id
            background_tasks.add_task(
                run_anomaly_checks_for_purchase, purchase_id, tenant_id
            )
    except Exception:
        pass

    return result


@router.post("/direct/{purchase_id}/attachments", dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def upload_direct_purchase_attachments_endpoint(
    purchase_id: UUID,
    request: Request,
    response: Response
):
    """
    Upload attachments for a direct purchase (Invoice and Payment Proofs).
    Expects multipart/form-data.
    Uses manual form parsing to avoid Pydantic validation errors on binary data.
    """
    try:
        form = await request.form()
        invoice_files = form.getlist("invoice_files")
        payment_files = form.getlist("payment_files")
    except Exception:
        invoice_files = []
        payment_files = []

    return await upload_direct_purchase_attachments(
        request=request,
        response=response,
        purchase_id=purchase_id,
        invoice_files=invoice_files,
        payment_files=payment_files
    )


@router.get("/suppliers/{supplier_id}/catalog", dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def get_supplier_catalog_endpoint(
    supplier_id: UUID,
    request: Request,
    response: Response
):
    """
    Get catalog prices for a specific supplier.
    Returns all ingredients with their purchase units and suggested prices.
    Used to pre-populate prices in the direct purchase form.
    """
    return await get_supplier_catalog_prices(
        request=request,
        response=response,
        supplier_id=supplier_id
    )


# =============================================================================
# REGULAR PURCHASES ENDPOINTS (Órdenes de Compra - WR-XXXX)
# =============================================================================

@router.get("", response_model=PurchasesListResponse, dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def get_purchases_endpoint(
    request: Request,
    response: Response,
    page: int = Query(default=1, ge=1, description="Page number"),
    limit: int = Query(default=50, ge=1, le=250, description="Items per page"),
    search: Optional[str] = Query(default=None, description="Search by purchase number or invoice number"),
    search_field: Optional[str] = Query(default=None, description="Field to search in (supplier_name, invoice_number, purchase_number)"),
    status: Optional[str] = Query(default=None, description="Filter by status"),
    supplier_id: Optional[UUID] = Query(default=None, description="Filter by supplier ID"),
    payment_status: Optional[str] = Query(default=None, description="Filter by payment status (pending, overdue, due_this_week)"),
    date_filter: Optional[str] = Query(default=None, description="Filter by date range (today, yesterday, last_week, 15_days, 1_month, 3_months)"),
    include_direct_payables: bool = Query(
        default=False,
        description="When true, also return direct credit purchases for Pagos (unpaid received + paid settlements; excludes contado)",
    ),
):
    """
    Get purchases list with tenant isolation
    Requires valid session with tenant context
    """
    return await get_purchases_list(
        request,
        response,
        page,
        limit,
        search,
        search_field,
        status,
        supplier_id,
        payment_status,
        date_filter,
        include_direct_payables=include_direct_payables,
    )

@router.get("/{purchase_id}", response_model=PurchaseResponse, dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def get_purchase_endpoint(
    purchase_id: UUID,
    request: Request,
    response: Response
):
    """
    Get a specific purchase by ID with tenant isolation
    """
    return await get_purchase_by_id(request, response, purchase_id)

@router.post("", response_model=PurchaseResponse, dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def create_purchase_endpoint(
    purchase_data: PurchaseCreate,
    request: Request,
    response: Response
):
    """
    Create a new purchase with tenant isolation
    """
    return await create_purchase(request, response, purchase_data)

@router.put("/{purchase_id}", response_model=PurchaseResponse, dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
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


# =============================================================================
# STATE TRANSITION ENDPOINTS
# =============================================================================

@router.post("/{purchase_id}/confirm", dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
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

@router.post("/{purchase_id}/ship", dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def ship_purchase_endpoint(
    purchase_id: UUID,
    request: Request,
    response: Response,
    tracking_number: str = Form(...),
    carrier: str = Form(...),
    estimated_delivery_date: Optional[str] = Form(None),
    package_count: Optional[int] = Form(None),
    notes: Optional[str] = Form(None),
    files: List[UploadFile] = File(None)
):
    """
    Transition purchase to SHIPPED state
    Records tracking number, carrier, and package information
    Accepts file attachments (shipping labels, photos, etc.)
    """
    return await transition_to_shipped(
        request=request,
        response=response,
        purchase_id=purchase_id,
        tracking_number=tracking_number,
        carrier=carrier,
        estimated_delivery_date=estimated_delivery_date,
        package_count=package_count,
        notes=notes,
        files=files
    )

@router.post("/{purchase_id}/receive", dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def receive_purchase_endpoint(
    purchase_id: UUID,
    request: Request,
    response: Response,
    items_data: str = Form(...),
    partial: bool = Form(False),
    all_items_approved: bool = Form(True),
    verification_notes: Optional[str] = Form(None),
    files: List[UploadFile] = File(None)
):
    """
    Transition purchase to RECEIVED or PARTIALLY_RECEIVED state
    Records quantities received and quality verification for each item
    Accepts file attachments (delivery photos, quality reports, etc.)
    """
    return await transition_to_received(
        request=request,
        response=response,
        purchase_id=purchase_id,
        items_data=items_data,
        partial=partial,
        all_items_approved=all_items_approved,
        verification_notes=verification_notes,
        files=files
    )


@router.post("/{purchase_id}/invoice", dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def invoice_purchase_endpoint(
    purchase_id: UUID,
    request: Request,
    response: Response,
    document_type: str = Form(...),
    invoice_number: str = Form(...),
    invoice_date: str = Form(...),
    invoice_amount: Optional[float] = Form(None),
    tax_amount: Optional[float] = Form(None),
    credit_days: Optional[int] = Form(None),
    payment_due_date: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    files: List[UploadFile] = File(None)
):
    """
    Transition purchase to INVOICED state
    Records invoice details and payment due date
    Accepts file attachments (invoices, receipts, etc.)
    """
    return await transition_to_invoiced(
        request=request,
        response=response,
        purchase_id=purchase_id,
        document_type=document_type,
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        invoice_amount=invoice_amount,
        tax_amount=tax_amount,
        credit_days=credit_days,
        payment_due_date=payment_due_date,
        notes=notes,
        files=files
    )

@router.post("/{purchase_id}/pay", dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def pay_purchase_endpoint(
    purchase_id: UUID,
    request: Request,
    response: Response,
    payment_method: str = Form(...),
    payment_method_id: Optional[UUID] = Form(None),
    payment_reference: str = Form(...),
    payment_amount: float = Form(...),
    payment_date: str = Form(...),
    notes: Optional[str] = Form(None),
    files: List[UploadFile] = File(None)
):
    """
    Transition purchase to PAID state
    Records payment method and reference
    Accepts file attachments (payment proofs, receipts, etc.)
    """
    return await transition_to_paid(
        request=request,
        response=response,
        purchase_id=purchase_id,
        payment_method=payment_method,
        payment_method_id=payment_method_id,
        payment_reference=payment_reference,
        payment_amount=payment_amount,
        payment_date=payment_date,
        notes=notes,
        files=files
    )

@router.post("/{purchase_id}/cancel", dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
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

@router.get("/{purchase_id}/history", response_model=StatusHistoryResponse, dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
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

@router.get("/{purchase_id}/transitions/{transition_id}", dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def get_transition_detail_endpoint(
    purchase_id: UUID,
    transition_id: UUID,
    request: Request,
    response: Response
):
    """
    Get detailed information about a specific transition
    Includes transition data, purchase number, and related attachments with presigned URLs
    """
    return await get_transition_detail(request, response, purchase_id, transition_id)

@router.get("/{purchase_id}/attachments", response_model=AttachmentsResponse, dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
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

@router.post("/{purchase_id}/transitions/{transition_id}/attachments", dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def create_transition_attachment_endpoint(
    purchase_id: UUID,
    transition_id: UUID,
    request: Request,
    response: Response,
    files: List[UploadFile] = File(...)
):
    """
    Upload attachments for a specific transition
    """
    from app.services.purchase_tracking_service import upload_transition_attachments
    return await upload_transition_attachments(request, response, purchase_id, transition_id, files)

@router.post("/{purchase_id}/attachments", dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
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

@router.post("/{purchase_id}/complete-quotation", dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
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

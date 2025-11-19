"""
Supplier Portal Router
Public endpoints for suppliers to access their portal using a unique token
No authentication required - token is used for identification
"""
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from uuid import UUID
from typing import Optional, List, Dict, Any
from pydantic import BaseModel
from app.services.supplier_portal_service import (
    verify_supplier_token,
    get_supplier_purchases,
    get_supplier_invoices,
    attach_legal_invoice,
    update_purchase_prices,
    invoice_purchase_from_portal,
    ship_purchase_from_portal
)

router = APIRouter()

# =============================================================================
# REQUEST/RESPONSE MODELS
# =============================================================================

class ItemPriceUpdate(BaseModel):
    id: str
    unit_cost: float
    notes: Optional[str] = None

class UpdatePricesRequest(BaseModel):
    items: List[ItemPriceUpdate]
    tax_amount: float
    notes: Optional[str] = None

class InvoicePurchaseRequest(BaseModel):
    document_type: str
    invoice_number: str
    invoice_date: str
    invoice_amount: Optional[float] = None
    tax_amount: Optional[float] = None
    credit_days: Optional[int] = None
    payment_due_date: Optional[str] = None
    notes: Optional[str] = None

class ShipPurchaseRequest(BaseModel):
    tracking_number: str
    carrier: str
    estimated_delivery_date: Optional[str] = None
    package_count: Optional[int] = None
    notes: Optional[str] = None

# =============================================================================
# PUBLIC ENDPOINTS (No authentication required)
# =============================================================================

@router.get("/{token}/verify")
async def verify_token_endpoint(token: str):
    """
    Verify if supplier token is valid
    Public endpoint - no authentication required
    """
    return await verify_supplier_token(token)

@router.get("/{token}/purchases")
async def get_purchases_endpoint(
    token: str,
    status: Optional[str] = None
):
    """
    Get all purchases for a supplier
    Public endpoint - token is used for identification

    Args:
        token: Supplier's unique access token
        status: Optional status filter (quotation, pending, confirmed, etc.)
    """
    return await get_supplier_purchases(token, status)

@router.get("/{token}/invoices")
async def get_invoices_endpoint(
    token: str,
    document_type: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None
):
    """
    Get all invoices/remisiones for a supplier
    Public endpoint - token is used for identification

    Args:
        token: Supplier's unique access token
        document_type: Optional filter by document type (factura, remision)
        start_date: Optional filter by start date (YYYY-MM-DD)
        end_date: Optional filter by end date (YYYY-MM-DD)
    """
    return await get_supplier_invoices(token, document_type, start_date, end_date)

@router.post("/{token}/invoices/attach-legal")
async def attach_legal_invoice_endpoint(
    token: str,
    purchase_ids: str = Form(...),
    legal_invoice_number: str = Form(...),
    legal_invoice_date: str = Form(...),
    files: List[UploadFile] = File(default=[])
):
    """
    Attach a legal invoice to multiple remisiones
    Public endpoint - token is used for identification

    Args:
        token: Supplier's unique access token
        purchase_ids: Comma-separated list of purchase IDs (remisiones)
        legal_invoice_number: Legal invoice number
        legal_invoice_date: Legal invoice date
        files: Attached invoice files
    """
    try:
        purchase_uuids = [UUID(pid.strip()) for pid in purchase_ids.split(',')]
    except ValueError:
        raise HTTPException(status_code=400, detail="IDs de compra inválidos")

    return await attach_legal_invoice(
        token=token,
        purchase_ids=purchase_uuids,
        legal_invoice_number=legal_invoice_number,
        legal_invoice_date=legal_invoice_date,
        files=files
    )

@router.post("/{token}/purchases/{purchase_id}/update-prices")
async def update_prices_endpoint(
    token: str,
    purchase_id: str,
    data: UpdatePricesRequest
):
    """
    Allow supplier to complete quotation with prices
    Public endpoint - token is used for identification

    Args:
        token: Supplier's unique access token
        purchase_id: Purchase ID to update
        data: Items with prices and totals
    """
    try:
        purchase_uuid = UUID(purchase_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="ID de compra inválido")

    items_dict = [item.dict() for item in data.items]

    return await update_purchase_prices(
        token=token,
        purchase_id=purchase_uuid,
        items_prices=items_dict,
        tax_amount=data.tax_amount,
        notes=data.notes
    )

@router.post("/{token}/purchases/{purchase_id}/invoice")
async def invoice_purchase_endpoint(
    token: str,
    purchase_id: str,
    document_type: str = Form(...),
    invoice_number: str = Form(...),
    invoice_date: str = Form(...),
    invoice_amount: Optional[float] = Form(None),
    tax_amount: Optional[float] = Form(None),
    credit_days: Optional[int] = Form(None),
    payment_due_date: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    files: List[UploadFile] = File(default=[])
):
    """
    Allow supplier to register invoice/remision with attachments
    Public endpoint - token is used for identification

    Args:
        token: Supplier's unique access token
        purchase_id: Purchase ID to invoice
        document_type: Type of document (remision, factura_contado, factura_credito)
        invoice_number: Invoice/remision number
        invoice_date: Date of invoice/remision
        invoice_amount: Amount (required for invoices)
        tax_amount: Tax amount
        credit_days: Credit days for factura_credito
        payment_due_date: Payment due date
        notes: Optional notes
        files: Attached files (invoices, receipts, etc.)
    """
    try:
        purchase_uuid = UUID(purchase_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="ID de compra inválido")

    return await invoice_purchase_from_portal(
        token=token,
        purchase_id=purchase_uuid,
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

@router.post("/{token}/purchases/{purchase_id}/ship")
async def ship_purchase_endpoint(
    token: str,
    purchase_id: str,
    tracking_number: str = Form(...),
    carrier: str = Form(...),
    estimated_delivery_date: Optional[str] = Form(None),
    package_count: Optional[int] = Form(None),
    notes: Optional[str] = Form(None),
    files: List[UploadFile] = File(default=[])
):
    """
    Allow supplier to mark purchase as shipped with attachments
    Public endpoint - token is used for identification

    Args:
        token: Supplier's unique access token
        purchase_id: Purchase ID to ship
        tracking_number: Tracking number from carrier
        carrier: Shipping carrier name
        estimated_delivery_date: Optional estimated delivery date
        package_count: Optional number of packages
        notes: Optional shipping notes
        files: Attached files (shipping labels, photos, etc.)
    """
    try:
        purchase_uuid = UUID(purchase_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="ID de compra inválido")

    return await ship_purchase_from_portal(
        token=token,
        purchase_id=purchase_uuid,
        tracking_number=tracking_number,
        carrier=carrier,
        estimated_delivery_date=estimated_delivery_date,
        package_count=package_count,
        notes=notes,
        files=files
    )

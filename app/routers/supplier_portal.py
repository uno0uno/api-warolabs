"""
Supplier Portal Router
Public endpoints for suppliers to access their portal using a unique token
No authentication required - token is used for identification
"""
from fastapi import APIRouter, HTTPException
from uuid import UUID
from typing import Optional, List, Dict, Any
from pydantic import BaseModel
from app.services.supplier_portal_service import (
    verify_supplier_token,
    get_supplier_purchases,
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
    data: InvoicePurchaseRequest
):
    """
    Allow supplier to register invoice/remision
    Public endpoint - token is used for identification

    Args:
        token: Supplier's unique access token
        purchase_id: Purchase ID to invoice
        data: Invoice/remision information
    """
    try:
        purchase_uuid = UUID(purchase_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="ID de compra inválido")

    return await invoice_purchase_from_portal(
        token=token,
        purchase_id=purchase_uuid,
        document_type=data.document_type,
        invoice_number=data.invoice_number,
        invoice_date=data.invoice_date,
        invoice_amount=data.invoice_amount,
        tax_amount=data.tax_amount,
        credit_days=data.credit_days,
        payment_due_date=data.payment_due_date,
        notes=data.notes
    )

@router.post("/{token}/purchases/{purchase_id}/ship")
async def ship_purchase_endpoint(
    token: str,
    purchase_id: str,
    data: ShipPurchaseRequest
):
    """
    Allow supplier to mark purchase as shipped
    Public endpoint - token is used for identification

    Args:
        token: Supplier's unique access token
        purchase_id: Purchase ID to ship
        data: Shipping information (tracking, carrier, etc.)
    """
    try:
        purchase_uuid = UUID(purchase_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="ID de compra inválido")

    return await ship_purchase_from_portal(
        token=token,
        purchase_id=purchase_uuid,
        tracking_number=data.tracking_number,
        carrier=data.carrier,
        estimated_delivery_date=data.estimated_delivery_date,
        package_count=data.package_count,
        notes=data.notes
    )

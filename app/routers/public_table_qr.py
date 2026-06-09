"""Public Table QR endpoints (api-warolabs#266, #267).

No authentication — used by warocol.com/{slug}/mesa/{token}.
"""
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.services import public_table_qr_service

router = APIRouter()


class TableQrRequestModifier(BaseModel):
    id: UUID
    quantity: int = Field(1, ge=1)


class TableQrRequestItem(BaseModel):
    product_id: UUID
    quantity: int = Field(..., gt=0)
    modifiers: Optional[List[TableQrRequestModifier]] = None
    notes: Optional[str] = None


class SubmitTableQrRequest(BaseModel):
    items: List[TableQrRequestItem] = Field(..., min_length=1)
    payment_method: Optional[str] = None
    payment_method_id: Optional[UUID] = None
    customer_notes: Optional[str] = None


@router.get("/{token}/menu")
async def get_table_qr_menu(
    token: str,
    category_id: Optional[UUID] = Query(default=None),
) -> Dict[str, Any]:
    """Products available for Table QR ordering (`is_available_table_qr`)."""
    menu = await public_table_qr_service.get_menu_for_token(token, category_id=category_id)
    return {"success": True, "data": menu}


@router.get("/{token}/product/{product_id}")
async def get_table_qr_product(
    token: str,
    product_id: UUID,
) -> Dict[str, Any]:
    """Product detail with modifier groups for Table QR checkout."""
    product = await public_table_qr_service.get_product_detail_for_token(token, product_id)
    return {"success": True, "data": product}


@router.get("/{token}/payment-methods")
async def get_table_qr_payment_methods(token: str) -> Dict[str, Any]:
    """Active payment groups for this table's tenant (no cartera groups)."""
    return await public_table_qr_service.get_payment_methods_for_token(token)


@router.post("/{token}/requests")
async def submit_table_qr_request(
    request: Request,
    token: str,
    body: SubmitTableQrRequest,
) -> Dict[str, Any]:
    """
    Submit a pending order for staff confirmation.

    Does not create orders, comandas, or POS tab items (#267).
    """
    items = [
        {
            "product_id": str(item.product_id),
            "quantity": item.quantity,
            "modifiers": [
                {"id": str(m.id), "quantity": m.quantity}
                for m in item.modifiers
            ] if item.modifiers else [],
            "notes": item.notes,
        }
        for item in body.items
    ]
    result = await public_table_qr_service.submit_table_qr_request(
        request,
        token,
        items=items,
        payment_method=body.payment_method,
        payment_method_id=body.payment_method_id,
        customer_notes=body.customer_notes,
    )
    return {"success": True, "data": result}


@router.get("/{token}")
async def resolve_table_qr(token: str) -> Dict[str, Any]:
    """
    Resolve a table QR public token to tenant slug, display name, and table name.

    Returns 404 when the token is unknown or the link is disabled
    (module off, qr_enabled=false, inactive profile/table).
    """
    data = await public_table_qr_service.resolve_table_qr_token(token)
    if not data:
        raise HTTPException(
            status_code=404,
            detail="QR link not found or inactive",
        )
    return {"success": True, "data": data}

"""
Support Documents Router — bridge to api-facturacion (issue #129)

DIAN support documents (documento soporte de pago a no obligados a facturar).

Note: These endpoints are stubs until api-facturacion implements
/support-document/emit and /support-document/adjust.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from typing import Any, Dict, Optional
from uuid import UUID
from pydantic import BaseModel, Field
from app.core.middleware import require_valid_session
from app.core.permissions import Module, require_module

router = APIRouter(prefix="/api/support-documents", tags=["Support Documents"])

_STUB_DETAIL = "Not yet available — api-facturacion endpoint pending"


class SupportDocumentEmitRequest(BaseModel):
    supplier_id: Optional[str] = Field(None, description="Supplier UUID")
    amount: Optional[float] = Field(None, description="Total amount of the support document")
    description: Optional[str] = Field(None, description="Transaction description")


class SupportDocumentAdjustRequest(BaseModel):
    reason: Optional[str] = Field(None, description="Reason for adjustment")
    amount: Optional[float] = Field(None, description="Adjusted amount")


@router.post("/emit", dependencies=[Depends(require_module(Module.FACTURACION))])
async def emit_support_document(
    request: Request,
    body: SupportDocumentEmitRequest,
) -> Dict[str, Any]:
    """
    Emit a DIAN support document for a payment to a non-invoicing supplier.

    Stub — returns 503 until api-facturacion /support-document/emit is implemented.
    """
    require_valid_session(request)
    raise HTTPException(status_code=503, detail=_STUB_DETAIL)


@router.post("/{doc_id}/adjust", dependencies=[Depends(require_module(Module.FACTURACION))])
async def adjust_support_document(
    request: Request,
    doc_id: UUID,
    body: SupportDocumentAdjustRequest,
) -> Dict[str, Any]:
    """
    Adjust (note of adjustment) an existing support document.

    Stub — returns 503 until api-facturacion /support-document/adjust is implemented.
    """
    require_valid_session(request)
    raise HTTPException(status_code=503, detail=_STUB_DETAIL)

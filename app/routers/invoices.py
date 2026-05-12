"""
Invoices Router — bridge to api-facturacion (issue #129)

Credit notes, debit notes, and RADIAN events for electronic invoices.

Note: These endpoints are stubs until api-facturacion implements the
corresponding upstream endpoints (/credit-note/emit, /debit-note/emit, /events).
When api-facturacion is ready, replace the 503 stub body with a call to
facturacion_service.proxy_to_facturacion().
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from typing import Any, Dict, Optional
from uuid import UUID
from pydantic import BaseModel, Field
from app.core.dependencies import require_invoicing_ready
from app.core.middleware import require_valid_session
from app.core.permissions import Module, require_module

router = APIRouter(prefix="/api/invoices", tags=["Invoices"])

_STUB_DETAIL = "Not yet available — api-facturacion endpoint pending"


class CreditNoteRequest(BaseModel):
    reason: Optional[str] = Field(None, description="Reason for credit note (correction or cancellation)")
    correction_concept: Optional[str] = Field(None, description="DIAN correction concept code")


class DebitNoteRequest(BaseModel):
    reason: Optional[str] = Field(None, description="Reason for debit note (additional charge)")


class RAdianEventRequest(BaseModel):
    event_code: str = Field(..., description="RADIAN event code: 030 | 031 | 032 | 033")


@router.post("/{invoice_id}/credit-note", dependencies=[Depends(require_module(Module.FACTURACION))])
async def emit_credit_note(
    request: Request,
    invoice_id: UUID,
    body: CreditNoteRequest,
    _readiness: dict = Depends(require_invoicing_ready),
) -> Dict[str, Any]:
    """
    Emit a DIAN credit note for an existing invoice (correction or cancellation).

    Returns 403 if the tenant is not ready for electronic invoicing
    (issue #130: missing dev flag, fiscal data, or active resolution).

    Stub — returns 503 until api-facturacion /credit-note/emit is implemented.
    """
    raise HTTPException(status_code=503, detail=_STUB_DETAIL)


@router.post("/{invoice_id}/debit-note", dependencies=[Depends(require_module(Module.FACTURACION))])
async def emit_debit_note(
    request: Request,
    invoice_id: UUID,
    body: DebitNoteRequest,
    _readiness: dict = Depends(require_invoicing_ready),
) -> Dict[str, Any]:
    """
    Emit a DIAN debit note for an existing invoice (additional charge).

    Returns 403 if the tenant is not ready for electronic invoicing
    (issue #130: missing dev flag, fiscal data, or active resolution).

    Stub — returns 503 until api-facturacion /debit-note/emit is implemented.
    """
    raise HTTPException(status_code=503, detail=_STUB_DETAIL)


@router.post("/{invoice_id}/events", dependencies=[Depends(require_module(Module.FACTURACION))])
async def emit_radian_event(
    request: Request,
    invoice_id: UUID,
    body: RAdianEventRequest,
) -> Dict[str, Any]:
    """
    Register a RADIAN event on an invoice (acknowledgement, receipt, acceptance, rejection).

    Event codes: 030 = acknowledgement, 031 = receipt, 032 = acceptance, 033 = rejection.
    Stub — returns 503 until api-facturacion /events is implemented.
    """
    require_valid_session(request)
    raise HTTPException(status_code=503, detail=_STUB_DETAIL)


@router.get("/{invoice_id}/events", dependencies=[Depends(require_module(Module.FACTURACION))])
async def get_radian_events(
    request: Request,
    invoice_id: UUID,
) -> Dict[str, Any]:
    """
    Get RADIAN events registered for an invoice.

    Stub — returns 503 until api-facturacion /events/{invoice_id} is implemented.
    """
    require_valid_session(request)
    raise HTTPException(status_code=503, detail=_STUB_DETAIL)

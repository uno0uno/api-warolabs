"""
Documents Router — bridge to api-facturacion (issue #129)

Document management: list, PDF/XML download, and email resend.

GET  /  and GET /{track_id}/pdf and GET /{track_id}/xml are live —
they read electronic_invoices directly from DB and generate R2 presigned URLs.

POST /{track_id}/resend-email is a stub until api-facturacion implements
the resend endpoint.
"""
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from typing import Any, Dict, Optional
from uuid import UUID
from app.core.middleware import require_valid_session
from app.core.permissions import Module, require_module
from app.services import facturacion_service

router = APIRouter(prefix="/api/documents", tags=["Document Management"])

_STUB_DETAIL = "Not yet available — api-facturacion endpoint pending"


@router.get("", dependencies=[Depends(require_module(Module.FACTURACION))])
async def list_documents(
    request: Request,
    prefix: Optional[str] = Query(None, description="Filter by invoice prefix (e.g. SETP)"),
    number: Optional[int] = Query(None, description="Filter by invoice number"),
    status: Optional[str] = Query(None, description="Filter by status: accepted, pending, rejected"),
    date_from: Optional[str] = Query(None, description="Start date YYYY-MM-DD"),
    date_to: Optional[str] = Query(None, description="End date YYYY-MM-DD"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    """
    List electronic invoices for the authenticated tenant.

    Optional filters: prefix, invoice number, status, date range.
    Returns paginated list with total count.
    """
    session = require_valid_session(request)
    return await facturacion_service.get_documents_list(
        tenant_id=str(session.tenant_id),
        prefix=prefix,
        number=number,
        status=status,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )


@router.post("/{track_id}/resend-email", dependencies=[Depends(require_module(Module.FACTURACION))])
async def resend_document_email(
    request: Request,
    track_id: UUID,
) -> Dict[str, Any]:
    """
    Resend the invoice email for an electronic document.

    Stub — returns 503 until api-facturacion /document/resend-email is implemented.
    """
    require_valid_session(request)
    raise HTTPException(status_code=503, detail=_STUB_DETAIL)


@router.get("/{track_id}/pdf", dependencies=[Depends(require_module(Module.FACTURACION))])
async def get_document_pdf(
    request: Request,
    track_id: UUID,
) -> Dict[str, Any]:
    """
    Get a fresh presigned URL for the PDF of an electronic invoice.

    track_id is the electronic_invoices.id (UUID).
    URL expires in 1 hour.
    """
    session = require_valid_session(request)
    pdf_url = await facturacion_service.get_document_pdf_url(
        track_id=str(track_id),
        tenant_id=str(session.tenant_id),
    )
    if pdf_url is None:
        raise HTTPException(status_code=404, detail="PDF not available for this invoice")
    return {"pdf_url": pdf_url, "expires_in": 3600}


@router.get("/{track_id}/xml", dependencies=[Depends(require_module(Module.FACTURACION))])
async def get_document_xml(
    request: Request,
    track_id: UUID,
) -> Dict[str, Any]:
    """
    Get a fresh presigned URL for the XML (AttachedDocument) of an electronic invoice.

    track_id is the electronic_invoices.id (UUID).
    URL expires in 1 hour.
    """
    session = require_valid_session(request)
    xml_url = await facturacion_service.get_document_xml_url(
        track_id=str(track_id),
        tenant_id=str(session.tenant_id),
    )
    if xml_url is None:
        raise HTTPException(status_code=404, detail="XML not available for this invoice")
    return {"xml_url": xml_url, "expires_in": 3600}

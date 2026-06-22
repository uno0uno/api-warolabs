"""
Documents Router — bridge to api-facturacion (issue #129)

Document management: list, PDF/XML download, and email send.

GET  /  and GET /{track_id}/xml read electronic_invoices directly from DB.
GET  /{track_id}/pdf uses R2 first and can fall back to Matias via api-facturacion.

POST /{track_id}/resend-email and POST /{track_id}/send-email-to proxy
to api-facturacion which talks to Matias. Both enforce tenant ownership
on the electronic_invoices row before forwarding (warocol.com#598).
"""
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from pydantic import BaseModel, EmailStr, Field

from app.core.middleware import require_valid_session
from app.core.permissions import Module, require_module
from app.database import get_db_connection
from app.services import facturacion_service

router = APIRouter(prefix="/api/documents", tags=["Document Management"])


async def _assert_invoice_belongs_to_tenant(track_id: UUID, tenant_id: UUID) -> None:
    """404 if the electronic_invoices row is not owned by the session tenant.

    Mirrors `facturacion_service.get_document_pdf_url` ownership pattern.
    Critical: api-facturacion does NOT enforce tenant scoping (no session
    there). Without this check a user from tenant A could trigger Matias
    to send tenant B's invoice anywhere they want.
    """
    async with get_db_connection(use_transaction=False) as conn:
        row = await conn.fetchrow(
            "SELECT id FROM electronic_invoices WHERE id = $1 AND tenant_id = $2",
            track_id, tenant_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Invoice not found")


class SendEmailToBody(BaseModel):
    email: EmailStr = Field(..., description="Recipient email address")
    name: Optional[str] = Field(None, description="Recipient display name (optional)")


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
    """List electronic invoices for the authenticated tenant."""
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
    """Resend the invoice email to the customer address registered on Matias."""
    session = require_valid_session(request)
    await _assert_invoice_belongs_to_tenant(track_id, session.tenant_id)
    return await facturacion_service.proxy_to_facturacion(
        method='POST',
        path=f'/documents/{track_id}/resend-email',
    )


@router.post("/{track_id}/send-email-to", dependencies=[Depends(require_module(Module.FACTURACION))])
async def send_document_email_to(
    request: Request,
    track_id: UUID,
    body: SendEmailToBody,
) -> Dict[str, Any]:
    """Send the invoice email to a custom recipient (e.g. an accountant)."""
    session = require_valid_session(request)
    await _assert_invoice_belongs_to_tenant(track_id, session.tenant_id)
    return await facturacion_service.proxy_to_facturacion(
        method='POST',
        path=f'/documents/{track_id}/send-email-to',
        payload={'email': body.email, 'name': body.name or ''},
    )


@router.get("/{track_id}/pdf", dependencies=[Depends(require_module(Module.FACTURACION))])
async def get_document_pdf(
    request: Request,
    track_id: UUID,
) -> Dict[str, Any]:
    """Get a fresh presigned URL for the PDF of an electronic invoice."""
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
    """Get a fresh presigned URL for the XML (AttachedDocument) of an electronic invoice."""
    session = require_valid_session(request)
    xml_url = await facturacion_service.get_document_xml_url(
        track_id=str(track_id),
        tenant_id=str(session.tenant_id),
    )
    if xml_url is None:
        raise HTTPException(status_code=404, detail="XML not available for this invoice")
    return {"xml_url": xml_url, "expires_in": 3600}

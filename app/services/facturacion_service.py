"""
Facturacion Service — bridge to api-facturacion microservice (issues #128, #129)

Communicates with api-facturacion over the internal Docker network.
Internal URL: http://api-facturacion:8001 (prod) / http://localhost:5002 (dev)

Endpoints proxied:
  POST /invoice/emit          → emit_invoice()
  GET  electronic_invoices DB → get_order_invoice(), get_dian_status()
  GET  electronic_invoices DB → get_documents_list()
  GET  R2 presigned URL       → get_document_pdf_url(), get_document_xml_url()
  Generic proxy helper        → proxy_to_facturacion()

DB-only functions do NOT call api-facturacion — they read electronic_invoices
directly and generate R2 presigned URLs locally (api-warolabs has R2 credentials).
"""
import httpx
import logging
from typing import Any, Dict, List, Optional
from uuid import UUID
from fastapi import HTTPException

from app.config import settings
from app.database import get_db_connection
from app.services.aws_s3_service import AWSS3Service

logger = logging.getLogger(__name__)


async def emit_invoice(
    order_id: str,
    tenant_id: str,
    order_type: str = 'pos',
) -> Dict[str, Any]:
    """
    Emit a DIAN electronic invoice for a completed POS order.

    Validates the order is completed, then delegates to api-facturacion.
    Idempotent at the gateway: short-circuits when an accepted invoice
    already exists for this order, and rejects re-tries when the previous
    attempt failed with an un-recoverable Matias error (warocol.com#589).

    Raises:
        HTTPException 422 — order not found, not completed, or missing resolution
        HTTPException 503 — api-facturacion unreachable or timed out
        HTTPException 409 — emission already in progress OR unrecoverable
                            previous rejection (Matias "ya validado")
    """
    # Verify order exists, belongs to tenant, and is completed
    async with get_db_connection(use_transaction=False) as conn:
        order = await conn.fetchrow(
            "SELECT id, status FROM orders WHERE id = $1 AND tenant_id = $2",
            UUID(order_id), UUID(tenant_id),
        )

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order['status'] != 'completed':
        raise HTTPException(
            status_code=422,
            detail=f"Invoice can only be emitted for completed orders (current status: {order['status']})",
        )

    # Pre-check existing electronic_invoices for this order (warocol.com#589).
    # - If the latest is `accepted` → return it, no Matias call.
    # - If the latest is `rejected` with the Matias "ya validado" signature
    #   → 409 short-circuit so the cashier doesn't burn another resolution
    #   number on a retry that will fail again. Recovery (fetch from Matias
    #   + persist) is out of this layer's scope — handled in api-facturacion
    #   in a follow-up.
    # - Any other rejection signature is allowed to retry (e.g., missing NIT
    #   that the cashier just fixed).
    async with get_db_connection(use_transaction=False) as conn:
        latest = await conn.fetchrow(
            """SELECT status, invoice_number, prefix, error_message
               FROM electronic_invoices
               WHERE order_id = $1 AND tenant_id = $2
               ORDER BY created_at DESC
               LIMIT 1""",
            UUID(order_id), UUID(tenant_id),
        )

    if latest and latest['status'] == 'accepted':
        existing = await get_order_invoice(order_id, tenant_id)
        if existing is not None:
            logger.info(f"Invoice already accepted for order {order_id}, short-circuiting emit")
            return existing

    if (
        latest
        and latest['status'] == 'rejected'
        and latest['error_message']
        and 'ya se encuentra validado' in latest['error_message'].lower()
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                f"La factura {latest['prefix']}{latest['invoice_number']} ya está "
                "validada en DIAN pero no se pudo descargar localmente. "
                "Contacta soporte para reconciliar el documento."
            ),
        )

    # Delegate to api-facturacion
    url = f"{settings.facturacion_api_url.rstrip('/')}/invoice/emit"
    payload = {
        'order_id': order_id,
        'tenant_id': tenant_id,
        'order_type': order_type,
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, json=payload)
    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        logger.error(f"api-facturacion unreachable for order {order_id}: {exc}")
        raise HTTPException(
            status_code=503,
            detail="Electronic invoicing service is temporarily unavailable. Please try again.",
        )

    data: Dict[str, Any] = {}
    try:
        data = resp.json()
    except Exception:
        data = {'raw': resp.text}

    if resp.status_code == 409:
        raise HTTPException(status_code=409, detail=data.get('detail', 'Invoice emission already in progress'))

    if not resp.is_success:
        detail = data.get('detail') or data.get('message') or resp.text[:300]
        logger.error(f"api-facturacion error {resp.status_code} for order {order_id}: {detail}")
        raise HTTPException(status_code=resp.status_code, detail=str(detail))

    logger.info(f"Invoice emitted for order {order_id}: status={data.get('status')} cufe={(data.get('cufe') or '')[:16]}...")
    return data


async def proxy_to_facturacion(
    method: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    """
    Generic proxy helper for forwarding requests to api-facturacion.

    Args:
        method:  HTTP method ('GET', 'POST', etc.)
        path:    Path relative to facturacion_api_url (e.g. '/credit-note/emit')
        payload: JSON body for POST requests
        timeout: Request timeout in seconds (default 30s per issue spec)

    Raises:
        HTTPException 503 — api-facturacion unreachable or timed out
        HTTPException <status> — api-facturacion returned a non-2xx response
    """
    url = f"{settings.facturacion_api_url.rstrip('/')}{path}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if method.upper() == 'GET':
                resp = await client.get(url)
            else:
                resp = await client.post(url, json=payload or {})
    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        logger.error(f"api-facturacion unreachable at {path}: {exc}")
        raise HTTPException(
            status_code=503,
            detail="Electronic invoicing service is temporarily unavailable. Please try again.",
        )

    data: Dict[str, Any] = {}
    try:
        data = resp.json()
    except Exception:
        data = {'raw': resp.text}

    if not resp.is_success:
        detail = data.get('detail') or data.get('message') or resp.text[:300]
        logger.error(f"api-facturacion error {resp.status_code} at {path}: {detail}")
        raise HTTPException(status_code=resp.status_code, detail=str(detail))

    return data


async def get_dian_status(
    order_id: str,
    tenant_id: str,
) -> Optional[Dict[str, Any]]:
    """
    Return the DIAN verification status for an order's invoice.

    Reads electronic_invoices directly from DB — no api-facturacion call.
    Returns None if no invoice exists for this order.
    """
    async with get_db_connection(use_transaction=False) as conn:
        row = await conn.fetchrow(
            """SELECT id, invoice_number, prefix, cufe, status,
                      error_message, emitted_at, created_at
               FROM electronic_invoices
               WHERE order_id = $1 AND tenant_id = $2
               ORDER BY created_at DESC
               LIMIT 1""",
            UUID(order_id), UUID(tenant_id),
        )

    if not row:
        return None

    return {
        'order_id': order_id,
        'invoice_id': str(row['id']),
        'invoice_number': row['invoice_number'],
        'prefix': row['prefix'],
        'cufe': row['cufe'],
        'status': row['status'],
        'error_message': row['error_message'],
        'emitted_at': row['emitted_at'].isoformat() if row['emitted_at'] else None,
        'created_at': row['created_at'].isoformat() if row['created_at'] else None,
    }


async def get_documents_list(
    tenant_id: str,
    prefix: Optional[str] = None,
    number: Optional[int] = None,
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    """
    Return a paginated list of electronic invoices for a tenant.

    Optional filters: prefix, invoice_number, status, date range.
    Reads electronic_invoices directly — no api-facturacion call.
    """
    conditions = ['tenant_id = $1']
    params: List[Any] = [UUID(tenant_id)]
    idx = 2

    if prefix is not None:
        conditions.append(f'prefix = ${idx}')
        params.append(prefix)
        idx += 1
    if number is not None:
        conditions.append(f'invoice_number = ${idx}')
        params.append(number)
        idx += 1
    if status is not None:
        conditions.append(f'status = ${idx}')
        params.append(status)
        idx += 1
    if date_from is not None:
        conditions.append(f'created_at >= ${idx}::date')
        params.append(date_from)
        idx += 1
    if date_to is not None:
        conditions.append(f"created_at < (${idx}::date + interval '1 day')")
        params.append(date_to)
        idx += 1

    where = ' AND '.join(conditions)

    async with get_db_connection(use_transaction=False) as conn:
        total_row = await conn.fetchrow(
            f'SELECT COUNT(*) AS total FROM electronic_invoices WHERE {where}',
            *params,
        )
        rows = await conn.fetch(
            f"""SELECT id, order_id, order_type, invoice_number, prefix,
                       cufe, status, error_message, emitted_at, created_at
                FROM electronic_invoices
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT ${idx} OFFSET ${idx + 1}""",
            *params, limit, offset,
        )

    items = [
        {
            'id': str(r['id']),
            'order_id': str(r['order_id']),
            'order_type': r['order_type'],
            'invoice_number': r['invoice_number'],
            'prefix': r['prefix'],
            'cufe': r['cufe'],
            'status': r['status'],
            'error_message': r['error_message'],
            'emitted_at': r['emitted_at'].isoformat() if r['emitted_at'] else None,
            'created_at': r['created_at'].isoformat() if r['created_at'] else None,
        }
        for r in rows
    ]

    return {
        'total': total_row['total'],
        'limit': limit,
        'offset': offset,
        'items': items,
    }


async def get_document_pdf_url(
    track_id: str,
    tenant_id: str,
) -> Optional[str]:
    """
    Return a fresh R2 presigned URL for the PDF of an invoice.

    track_id maps to electronic_invoices.id (UUID primary key).
    Returns None if the invoice has no PDF yet.
    Raises HTTPException 404 if the invoice record does not exist.
    """
    async with get_db_connection(use_transaction=False) as conn:
        row = await conn.fetchrow(
            """SELECT status, r2_pdf_key
               FROM electronic_invoices
               WHERE id = $1 AND tenant_id = $2""",
            UUID(track_id), UUID(tenant_id),
        )

    if not row:
        raise HTTPException(status_code=404, detail="Invoice not found")

    if not row['r2_pdf_key']:
        return None

    try:
        s3 = AWSS3Service()
        return await s3.get_presigned_url(row['r2_pdf_key'], expiration=3600)
    except Exception as exc:
        logger.warning(f"Could not generate PDF presigned URL for invoice {track_id}: {exc}")
        return None


async def get_document_xml_url(
    track_id: str,
    tenant_id: str,
) -> Optional[str]:
    """
    Return a fresh R2 presigned URL for the XML of an invoice.

    track_id maps to electronic_invoices.id (UUID primary key).
    Returns None if the invoice has no XML yet.
    Raises HTTPException 404 if the invoice record does not exist.
    """
    async with get_db_connection(use_transaction=False) as conn:
        row = await conn.fetchrow(
            """SELECT status, r2_xml_key
               FROM electronic_invoices
               WHERE id = $1 AND tenant_id = $2""",
            UUID(track_id), UUID(tenant_id),
        )

    if not row:
        raise HTTPException(status_code=404, detail="Invoice not found")

    if not row['r2_xml_key']:
        return None

    try:
        s3 = AWSS3Service()
        return await s3.get_presigned_url(row['r2_xml_key'], expiration=3600)
    except Exception as exc:
        logger.warning(f"Could not generate XML presigned URL for invoice {track_id}: {exc}")
        return None


async def get_order_invoice(
    order_id: str,
    tenant_id: str,
) -> Optional[Dict[str, Any]]:
    """
    Return the current invoice record for an order, with a fresh presigned PDF URL.

    Reads electronic_invoices directly from DB (no api-facturacion call).
    Returns None if no invoice exists for this order.
    """
    async with get_db_connection(use_transaction=False) as conn:
        row = await conn.fetchrow(
            """SELECT id, invoice_number, prefix, cufe, status,
                      r2_pdf_key, error_message, emitted_at, created_at
               FROM electronic_invoices
               WHERE order_id = $1 AND tenant_id = $2
               ORDER BY created_at DESC
               LIMIT 1""",
            UUID(order_id), UUID(tenant_id),
        )

    if not row:
        return None

    pdf_presigned_url: Optional[str] = None
    if row['status'] == 'accepted' and row['r2_pdf_key']:
        try:
            s3 = AWSS3Service()
            pdf_presigned_url = await s3.get_presigned_url(row['r2_pdf_key'], expiration=3600)
        except Exception as exc:
            logger.warning(f"Could not generate presigned URL for invoice {row['id']}: {exc}")

    return {
        'order_id': order_id,
        'invoice_number': row['invoice_number'],
        'prefix': row['prefix'],
        'cufe': row['cufe'],
        'status': row['status'],
        'pdf_presigned_url': pdf_presigned_url,
        'error_message': row['error_message'],
        'emitted_at': row['emitted_at'].isoformat() if row['emitted_at'] else None,
        'created_at': row['created_at'].isoformat() if row['created_at'] else None,
    }

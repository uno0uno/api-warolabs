"""
Facturacion Service — bridge to api-facturacion microservice (issue #128)

Communicates with api-facturacion over the internal Docker network.
Internal URL: http://api-facturacion:8001 (prod) / http://localhost:5002 (dev)

Endpoints proxied:
  POST /invoice/emit  → emit_invoice()
  GET  electronic_invoices directly from DB → get_order_invoice()

The GET does NOT call api-facturacion — it reads the DB directly and generates
the R2 presigned URL locally (api-warolabs already has R2 credentials).
"""
import httpx
import logging
from typing import Any, Dict, Optional
from uuid import UUID
from fastapi import HTTPException

from app.config import settings
from app.database import get_db_connection
from app.core.middleware import require_valid_session
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
    Idempotent: calling twice returns the same accepted invoice.

    Raises:
        HTTPException 422 — order not found, not completed, or missing resolution
        HTTPException 503 — api-facturacion unreachable or timed out
        HTTPException 409 — emission already in progress
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

    logger.info(f"Invoice emitted for order {order_id}: status={data.get('status')} cufe={data.get('cufe', '')[:16]}...")
    return data


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

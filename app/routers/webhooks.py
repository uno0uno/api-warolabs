"""
Webhooks Router — public bridge for external webhook providers.

POST /api/webhooks/matias  → proxies raw body + signature header to
                             api-facturacion /webhooks/matias (internal Docker network).

No session auth — Matias calls this from the internet.
Raw body is forwarded unmodified so api-facturacion can verify HMAC-SHA256.
"""
import json
import httpx
import logging
from uuid import UUID
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from app.config import settings
from app.database import get_db_connection

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/webhooks", tags=["Webhooks"])


@router.post("/matias")
async def matias_webhook_bridge(request: Request):
    """
    Receive Matias webhook and proxy to api-facturacion.

    Forwards raw body + X-Matias-Signature header unchanged.
    Always returns 200 to prevent Matias retry storms.

    Defensive gap check (warocol.com#592): if the webhook references a
    `(tenant, prefix, number)` that we already logged as a permanent skip
    in `dian_sequence_gaps`, we log a WARNING so the audit trail surfaces
    the suspicious reconciliation attempt. Authoritative dedup must happen
    in api-facturacion (which owns the electronic_invoices write path) —
    this layer is just an early alarm.
    """
    body = await request.body()
    signature = request.headers.get("X-Matias-Signature", "")

    # warocol.com#592 — best-effort gap correlation. Wrapped in broad
    # try/except: this MUST NOT interfere with the proxy to api-facturacion.
    try:
        payload = json.loads(body) if body else {}
        invoice_number = payload.get("invoice_number")
        prefix = payload.get("prefix")
        tenant_id_hint = payload.get("tenant_id")
        if invoice_number and prefix and tenant_id_hint:
            async with get_db_connection(use_transaction=False) as conn:
                gap = await conn.fetchrow(
                    """SELECT 1 FROM dian_sequence_gaps
                       WHERE tenant_id = $1 AND prefix = $2 AND skipped_number = $3
                       LIMIT 1""",
                    UUID(str(tenant_id_hint)), str(prefix), int(invoice_number),
                )
            if gap:
                logger.warning(
                    "Matias webhook for %s%s references a logged sequence gap "
                    "for tenant %s — api-facturacion must dedup, not persist.",
                    prefix, invoice_number, tenant_id_hint,
                )
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.debug("Webhook gap-check best-effort failed: %s", exc)

    url = f"{settings.facturacion_api_url.rstrip('/')}/webhooks/matias"

    try:
        headers = {"Content-Type": "application/json"}
        if signature:
            headers["X-Matias-Signature"] = signature

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, content=body, headers=headers)

        logger.info(f"Matias webhook proxied to api-facturacion: {resp.status_code}")
    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        logger.error(f"Failed to proxy Matias webhook to api-facturacion: {exc}")

    # Always 200 — never cause Matias to retry
    return JSONResponse(content={"status": "ok"}, status_code=200)

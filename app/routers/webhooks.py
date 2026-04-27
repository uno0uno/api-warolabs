"""
Webhooks Router — public bridge for external webhook providers.

POST /api/webhooks/matias  → proxies raw body + signature header to
                             api-facturacion /webhooks/matias (internal Docker network).

No session auth — Matias calls this from the internet.
Raw body is forwarded unmodified so api-facturacion can verify HMAC-SHA256.
"""
import httpx
import logging
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/webhooks", tags=["Webhooks"])


@router.post("/matias")
async def matias_webhook_bridge(request: Request):
    """
    Receive Matias webhook and proxy to api-facturacion.

    Forwards raw body + X-Matias-Signature header unchanged.
    Always returns 200 to prevent Matias retry storms.
    """
    body = await request.body()
    signature = request.headers.get("X-Matias-Signature", "")

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

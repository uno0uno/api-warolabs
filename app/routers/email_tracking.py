"""Public email tracking pixel endpoint (api-warolabs#657).

No authentication — the URL is embedded in invoice emails as a 1x1 GIF.
Response is byte-identical for valid and invalid tokens: unknown tokens are a
silent no-op so the endpoint never leaks token validity.
"""
from fastapi import APIRouter, Response

from app.services import invoice_email_tracking_service

router = APIRouter()


@router.get("/{token}.gif")
async def email_tracking_pixel(token: str) -> Response:
    """Record an open signal and return the 1x1 tracking GIF.

    The raw token is never persisted — only its SHA-256 hash is looked up.
    No IP or user-agent is stored. The same 200 + GIF + no-store headers are
    returned whether the token exists or not.
    """
    token_hash = invoice_email_tracking_service.hash_tracking_token(token)
    await invoice_email_tracking_service.record_pixel_open(token_hash)
    return Response(
        content=invoice_email_tracking_service.PIXEL_GIF_BYTES,
        headers=dict(invoice_email_tracking_service.PIXEL_HEADERS),
    )

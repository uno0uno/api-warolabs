"""Public Table QR resolve endpoint (api-warolabs#266).

No authentication — used by warocol.com/{slug}/mesa/{token} before checkout.
"""
from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from app.services import public_table_qr_service

router = APIRouter()


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

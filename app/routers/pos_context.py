"""POS Context Router.

Exposes a single read-only aggregator that POS pages need at load and at
checkout: display name + KDS/comandas toggles + fiscal data + tax flags +
invoicing readiness. Gated under Module.POS so cashiers reach it without
needing MI_NEGOCIO (which stays owner-only by business rule).

This is the BFF-style alternative to having POS pages fan out to multiple
`/api/tenant/*` endpoints — see audit doc §4 for the rationale.
"""
from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.middleware import require_valid_session
from app.core.permissions import Module, require_module
from app.services.pos_context_service import get_restaurant_context

router = APIRouter(prefix="/pos", tags=["POS Context"])


@router.get(
    "/restaurant-context",
    dependencies=[Depends(require_module(Module.POS))],
)
async def get_pos_restaurant_context(request: Request):
    """Aggregated tenant context for POS pages (display, fiscal, tax, invoicing)."""
    session = require_valid_session(request)
    payload = await get_restaurant_context(session.tenant_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return {"success": True, "data": payload}

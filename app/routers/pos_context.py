"""POS Context Router.

Exposes a single read-only aggregator that POS pages need at load and at
checkout: display name + KDS/comandas toggles + fiscal data + tax flags +
invoicing readiness. Gated under Module.POS so cashiers reach it without
needing MI_NEGOCIO (which stays owner-only by business rule).

This is the BFF-style alternative to having POS pages fan out to multiple
`/api/tenant/*` endpoints — see audit doc §4 for the rationale.

Also exposes the per-session waiter mutation endpoint (warocol.com#574)
under Module.POS so cashiers can hand off / take a table from the POS
banner without needing OPERACIONES.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.middleware import require_valid_session
from app.core.permissions import Module, require_module
from app.models.table_member_assignment import SetOrderServedByRequest, SetSessionWaiterRequest
from app.services import table_assignments_service
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


# ── Waiter attribution (warocol.com#574) ─────────────────────────────

@router.patch(
    "/tables/{table_id}/session-waiter",
    dependencies=[Depends(require_module(Module.POS))],
)
async def set_session_waiter_endpoint(
    request: Request,
    table_id: UUID,
    body: SetSessionWaiterRequest,
):
    """Set or clear the waiter attributed to a table's open session.

    Auto-handoff guard applies (warocol.com#574):
      - 403 if caller is not the current waiter AND not supervisor+.
      - 404 if no open session for the table.
      - 404 if `member_id` doesn't belong to this tenant.
      - 409 if `waiter_attribution_enabled` is off for the tenant.
    """
    session = require_valid_session(request)
    return await table_assignments_service.set_session_waiter(
        tenant_id=session.tenant_id,
        table_id=table_id,
        member_id=body.member_id,
        caller_user_id=session.user_id,
        caller_role=session.role,
    )


@router.patch(
    "/orders/{order_id}/served-by",
    dependencies=[Depends(require_module(Module.POS))],
)
async def set_order_served_by_endpoint(
    request: Request,
    order_id: UUID,
    body: SetOrderServedByRequest,
):
    """Set or clear the per-order waiter (warocol.com#575).

    Used for bar/counter orders post-creation. Auto-handoff guard:
      - 403 if caller is not the current served_by AND not supervisor+.
      - 404 if the order doesn't exist or doesn't belong to this tenant.
      - 404 if `member_id` doesn't belong to this tenant.
      - 409 if `waiter_attribution_enabled` is off for the tenant.

    To set the value at creation time, include `served_by_member_id` in
    the body of POST /pos-cart/{id}/complete instead.
    """
    session = require_valid_session(request)
    return await table_assignments_service.set_order_served_by(
        tenant_id=session.tenant_id,
        order_id=order_id,
        member_id=body.member_id,
        caller_user_id=session.user_id,
        caller_role=session.role,
    )

"""Operaciones Context Router.

Audience-scoped aggregator + write surface for the operaciones pages
(/operaciones/{comandas,mesas,personalizar}). Mirrors pos_context.py but
gated under Module.OPERACIONES so supervisor/admin can read AND toggle
operational features without needing MI_NEGOCIO (which stays owner-only).

Five dedicated PATCH endpoints (one per toggle) — REST-friendly and easier
to gate / observe than a single combined endpoint. The service layer's
column whitelist guards against SQL injection.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from app.core.middleware import require_valid_session
from app.core.permissions import Module, require_module
from app.models.table_member_assignment import AssignMemberRequest
from app.services import table_assignments_service
from app.services.operaciones_context_service import (
    get_operaciones_context,
    update_toggle,
)

router = APIRouter(prefix="/operaciones", tags=["Operaciones Context"])


class ToggleRequest(BaseModel):
    enabled: bool


@router.get(
    "/restaurant-context",
    dependencies=[Depends(require_module(Module.OPERACIONES))],
)
async def get_operaciones_restaurant_context(request: Request):
    """Aggregated tenant context for operaciones pages.

    Same payload as the POS aggregator — supervisor/admin pages read the
    same toggles and metadata as cashiers do.
    """
    session = require_valid_session(request)
    payload = await get_operaciones_context(session.tenant_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return {"success": True, "data": payload}


@router.patch(
    "/toggles/kds",
    dependencies=[Depends(require_module(Module.OPERACIONES))],
)
async def toggle_kds_enabled(request: Request, body: ToggleRequest):
    """Toggle `kds_enabled` on the tenant profile."""
    session = require_valid_session(request)
    return await update_toggle(session.tenant_id, "kds_enabled", body.enabled)


@router.patch(
    "/toggles/comandas",
    dependencies=[Depends(require_module(Module.OPERACIONES))],
)
async def toggle_comandas_enabled(request: Request, body: ToggleRequest):
    """Toggle `comandas_enabled` on the tenant profile."""
    session = require_valid_session(request)
    return await update_toggle(session.tenant_id, "comandas_enabled", body.enabled)


@router.patch(
    "/toggles/expediter",
    dependencies=[Depends(require_module(Module.OPERACIONES))],
)
async def toggle_expediter_enabled(request: Request, body: ToggleRequest):
    """Toggle `expediter_enabled` on the tenant profile."""
    session = require_valid_session(request)
    return await update_toggle(session.tenant_id, "expediter_enabled", body.enabled)


@router.patch(
    "/toggles/tables",
    dependencies=[Depends(require_module(Module.OPERACIONES))],
)
async def toggle_tables_enabled(request: Request, body: ToggleRequest):
    """Toggle `tables_enabled` on the tenant profile."""
    session = require_valid_session(request)
    return await update_toggle(session.tenant_id, "tables_enabled", body.enabled)


@router.patch(
    "/toggles/auto-select-generic",
    dependencies=[Depends(require_module(Module.OPERACIONES))],
)
async def toggle_auto_select_generic(request: Request, body: ToggleRequest):
    """Toggle `auto_select_generic_enabled` on the tenant profile."""
    session = require_valid_session(request)
    return await update_toggle(
        session.tenant_id, "auto_select_generic_enabled", body.enabled
    )


# ── Waiter attribution family (warocol.com#573) ─────────────────────────

@router.patch(
    "/toggles/waiter-attribution",
    dependencies=[Depends(require_module(Module.OPERACIONES))],
)
async def toggle_waiter_attribution_enabled(request: Request, body: ToggleRequest):
    """Toggle `waiter_attribution_enabled` on the tenant profile.

    Controls the visibility of the waiter assignment family of features
    (admin panel here in #573, POS mesa override in #574, bar/counter
    order attribution in #575).
    """
    session = require_valid_session(request)
    return await update_toggle(
        session.tenant_id, "waiter_attribution_enabled", body.enabled
    )


@router.patch(
    "/tables/{table_id}/assigned-member",
    dependencies=[Depends(require_module(Module.OPERACIONES))],
)
async def assign_member_to_table_endpoint(
    request: Request,
    table_id: UUID,
    body: AssignMemberRequest,
):
    """Set or clear the default waiter for a table (warocol.com#573).

    Atomic: closes the previous open period in the history table, opens
    a new one with member snapshots, and updates the fast pointer on
    `tables.assigned_member_id`. Rejects bar tables (400), unknown
    tables/members (404), and when the feature flag is off (409).
    """
    session = require_valid_session(request)
    return await table_assignments_service.assign_member_to_table(
        tenant_id=session.tenant_id,
        table_id=table_id,
        member_id=body.member_id,
        assigned_by_user_id=session.user_id,
    )


@router.get(
    "/tables/{table_id}/assignment-history",
    dependencies=[Depends(require_module(Module.OPERACIONES))],
)
async def get_table_assignment_history(
    request: Request,
    table_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Paginated history of waiter assignments for a table.

    Most recent first. Includes denormalized snapshots of member name /
    role so the response is correct even if the member was deleted
    after the assignment.
    """
    session = require_valid_session(request)
    return await table_assignments_service.get_assignment_history(
        tenant_id=session.tenant_id,
        table_id=table_id,
        limit=limit,
        offset=offset,
    )

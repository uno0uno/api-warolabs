"""Operaciones Context Router.

Audience-scoped aggregator + write surface for the operaciones pages
(/operaciones/{comandas,mesas,personalizar}). Mirrors pos_context.py but
gated under Module.OPERACIONES so supervisor/admin can read AND toggle
operational features without needing MI_NEGOCIO (which stays owner-only).

Five dedicated PATCH endpoints (one per toggle) — REST-friendly and easier
to gate / observe than a single combined endpoint. The service layer's
column whitelist guards against SQL injection.
"""
from decimal import Decimal
from uuid import UUID

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.middleware import require_valid_session
from app.core.permissions import Module, require_module
from app.models.table_member_assignment import AssignMemberRequest
from app.services import table_assignments_service
from app.services.operaciones_context_service import (
    get_operaciones_context,
    update_tables_label,
    update_tip_config,
    update_toggle,
)

router = APIRouter(prefix="/operaciones", tags=["Operaciones Context"])


class ToggleRequest(BaseModel):
    enabled: bool


class TablesLabelRequest(BaseModel):
    """Payload for PATCH /operaciones/labels/tables (warocol.com#614).

    Both fields optional and accept empty strings — the service normalizes
    empty/whitespace input to NULL so users can reset to defaults by
    clearing both inputs.
    """
    singular: Optional[str] = Field(None, max_length=40)
    plural: Optional[str] = Field(None, max_length=40)


class TipConfigRequest(BaseModel):
    """Payload for PATCH /operaciones/tip/config (warocol.com#638).

    Non-boolean part of the tipping configuration. The boolean toggle
    (tip_enabled) flows through PATCH /operaciones/toggles/tip via the
    generic update_toggle() helper.

    Mirrors the validation rules baked into the DB CHECK constraints from
    migration 078 + the @field_validator on TenantPublicProfileBase:
        - 1 to 5 percentage entries, each in [0, 100]
        - preselect_index is NULL or a valid index into percentages
    """
    percentages: List[Decimal] = Field(..., min_length=1, max_length=5)
    preselect_index: Optional[int] = None

    @field_validator('percentages')
    @classmethod
    def _validate_percentages(cls, v):
        for p in v:
            if p < 0 or p > 100:
                raise ValueError(f"tip preset {p} must be between 0 and 100")
        return v

    @model_validator(mode='after')
    def _validate_preselect(self):
        if self.preselect_index is None:
            return self
        if self.preselect_index < 0:
            raise ValueError("preselect_index must be non-negative")
        if self.preselect_index >= len(self.percentages):
            raise ValueError(
                f"preselect_index {self.preselect_index} is out of bounds for "
                f"percentages of length {len(self.percentages)}"
            )
        return self


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
    "/toggles/table-qr",
    dependencies=[Depends(require_module(Module.OPERACIONES))],
)
async def toggle_table_qr_module(request: Request, body: ToggleRequest):
    """Toggle `table_qr_module_enabled` on the tenant profile (warocol.com#710)."""
    session = require_valid_session(request)
    return await update_toggle(session.tenant_id, "table_qr_module_enabled", body.enabled)


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


# ── Tipping family (warocol.com#638) ────────────────────────────────────

@router.patch(
    "/toggles/tip",
    dependencies=[Depends(require_module(Module.OPERACIONES))],
)
async def toggle_tip_enabled(request: Request, body: ToggleRequest):
    """Toggle `tip_enabled` on the tenant profile.

    Master switch for the tipping feature (warocol.com#638). When false,
    the checkout selector (POS + online) is hidden and the API rejects
    tip_amount > 0. Reads/writes of the existing tip presets remain
    accessible — operators can keep their preset list ready without
    surfacing tipping to customers.
    """
    session = require_valid_session(request)
    return await update_toggle(session.tenant_id, "tip_enabled", body.enabled)


@router.patch(
    "/toggles/tip-taxable-default",
    dependencies=[Depends(require_module(Module.OPERACIONES))],
)
async def toggle_tip_taxable_default(request: Request, body: ToggleRequest):
    """Toggle default taxable tip (gravada) at checkout (warocol.com#740).

    When true, new checkouts pre-select applying IVA/INC to the tip amount.
    Cashiers can still override per sale at checkout.
    """
    session = require_valid_session(request)
    return await update_toggle(session.tenant_id, "tip_taxable_default", body.enabled)


@router.patch(
    "/tip/config",
    dependencies=[Depends(require_module(Module.OPERACIONES))],
)
async def patch_tip_config(request: Request, body: TipConfigRequest):
    """Persist tip presets + preselected index (warocol.com#638).

    Non-boolean sibling of /operaciones/toggles/tip. Validates that the
    presets array is non-empty + <= 5 entries each in [0, 100], and that
    preselect_index (when set) is in bounds. App-level validation mirrors
    the DB CHECK constraints from migration 078.
    """
    session = require_valid_session(request)
    return await update_tip_config(
        session.tenant_id, body.percentages, body.preselect_index,
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


# ── Custom mesa label (warocol.com#614) ─────────────────────────────────

@router.patch(
    "/labels/tables",
    dependencies=[Depends(require_module(Module.OPERACIONES))],
)
async def patch_tables_label(request: Request, body: TablesLabelRequest):
    """Persist tenant-global custom labels for the 'Mesa' noun.

    Empty / whitespace input on either field is normalized server-side to
    NULL — the frontend interprets that as "use default" (Mesa / Mesas).
    """
    session = require_valid_session(request)
    return await update_tables_label(
        session.tenant_id, body.singular, body.plural,
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

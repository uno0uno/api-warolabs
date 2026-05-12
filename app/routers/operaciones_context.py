"""Operaciones Context Router.

Audience-scoped aggregator + write surface for the operaciones pages
(/operaciones/{comandas,mesas,personalizar}). Mirrors pos_context.py but
gated under Module.OPERACIONES so supervisor/admin can read AND toggle
operational features without needing MI_NEGOCIO (which stays owner-only).

Five dedicated PATCH endpoints (one per toggle) — REST-friendly and easier
to gate / observe than a single combined endpoint. The service layer's
column whitelist guards against SQL injection.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.core.middleware import require_valid_session
from app.core.permissions import Module, require_module
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

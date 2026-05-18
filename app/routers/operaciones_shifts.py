"""Shift template CRUD for Operaciones (warocol.com#682, #684)."""
from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.core.permissions import Module, require_module
from app.models.shift_template import ShiftTemplateCreate, ShiftTemplatePatch
from app.services import shift_templates_service, shift_window_service

router = APIRouter(prefix="/operaciones", tags=["Operaciones Shifts"])


@router.get(
    "/shifts",
    dependencies=[Depends(require_module(Module.OPERACIONES))],
)
async def list_shift_templates_endpoint(
    request: Request,
    include_inactive: bool = Query(
        False,
        description="When true, include deactivated templates (admin settings UI).",
    ),
):
    """List shift templates for the tenant (active only by default)."""
    return await shift_templates_service.list_shift_templates(
        request,
        include_inactive=include_inactive,
    )


@router.post(
    "/shifts",
    dependencies=[Depends(require_module(Module.OPERACIONES))],
)
async def create_shift_template_endpoint(
    request: Request,
    body: ShiftTemplateCreate,
):
    """Create a reusable shift template."""
    return await shift_templates_service.create_shift_template(request, body)


@router.get(
    "/shifts/{template_id}/window",
    dependencies=[Depends(require_module(Module.OPERACIONES))],
)
async def get_shift_template_window_endpoint(
    request: Request,
    template_id: UUID,
    anchor_date: date = Query(..., alias="date", description="Anchor calendar date (YYYY-MM-DD, Bogotá)"),
):
    """Resolve template clock times to periodStart/End + periodStartTime/EndTime."""
    return await shift_window_service.get_template_window(
        request, template_id, anchor_date
    )


@router.patch(
    "/shifts/{template_id}",
    dependencies=[Depends(require_module(Module.OPERACIONES))],
)
async def patch_shift_template_endpoint(
    request: Request,
    template_id: UUID,
    body: ShiftTemplatePatch,
):
    """Update fields or soft-deactivate (is_active=false)."""
    return await shift_templates_service.patch_shift_template(
        request, template_id, body
    )

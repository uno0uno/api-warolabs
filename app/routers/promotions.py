"""Tenant promotion CRUD and POS active read (warocol.com#980)."""
from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.core.permissions import Module, require_module
from app.models.tenant_promotion import PromotionCreate, PromotionDeleteRequest, PromotionUpdate
from app.services import promotions_service

router = APIRouter(prefix="/api/promotions", tags=["promotions"])

_CONFLICT_DOC = promotions_service.CONFLICT_RULES_DOC


@router.get(
    "",
    dependencies=[Depends(require_module(Module.MI_NEGOCIO))],
    summary="List tenant promotions",
    description=(
        "Returns promotions for the current tenant ordered by priority (desc). "
        f"Conflict rules: {_CONFLICT_DOC}"
    ),
)
async def list_promotions_endpoint(
    request: Request,
    include_inactive: bool = Query(
        False, description="Include deactivated promotions (admin)."
    ),
    at: Optional[datetime] = Query(
        None,
        description="Optional ISO timestamp; when set, each row includes is_currently_active.",
    ),
):
    return await promotions_service.list_promotions(
        request,
        include_inactive=include_inactive,
        at=at,
    )


@router.post(
    "",
    status_code=201,
    dependencies=[Depends(require_module(Module.MI_NEGOCIO))],
    summary="Create promotion",
    description=f"Create a tenant promotion. {_CONFLICT_DOC}",
)
async def create_promotion_endpoint(request: Request, body: PromotionCreate):
    return await promotions_service.create_promotion(request, body)


@router.get(
    "/active",
    dependencies=[Depends(require_module(Module.POS))],
    summary="List promotions with active status for POS",
    description=(
        "Read-only endpoint for POS. Evaluates schedules in the tenant operational timezone "
        "with America/Bogota fallback. "
        f"{_CONFLICT_DOC}"
    ),
)
async def list_active_promotions_endpoint(
    request: Request,
    at: Optional[datetime] = Query(
        None,
        description=(
            "Evaluation timestamp (ISO). Defaults to now in the tenant operational timezone."
        ),
    ),
    only_current: bool = Query(
        False,
        description="When true, return only promotions active at `at`.",
    ),
):
    return await promotions_service.list_active_promotions(
        request,
        at,
        only_current=only_current,
    )


@router.get(
    "/{promotion_id}",
    dependencies=[Depends(require_module(Module.MI_NEGOCIO))],
    summary="Get promotion by id",
)
async def get_promotion_endpoint(
    request: Request,
    promotion_id: UUID,
    at: Optional[datetime] = Query(None),
):
    return await promotions_service.get_promotion(request, promotion_id, at=at)


@router.get(
    "/{promotion_id}/scope",
    dependencies=[Depends(require_module(Module.MI_NEGOCIO))],
    summary="Paginated promotion scope items",
    description="Returns product or category names in promotion scope with optional search.",
)
async def list_promotion_scope_endpoint(
    request: Request,
    promotion_id: UUID,
    search: Optional[str] = Query(None, description="Filter names within scope."),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
):
    return await promotions_service.list_promotion_scope(
        request,
        promotion_id,
        search=search,
        page=page,
        page_size=page_size,
    )


@router.patch(
    "/{promotion_id}",
    dependencies=[Depends(require_module(Module.MI_NEGOCIO))],
    summary="Update promotion",
)
async def update_promotion_endpoint(
    request: Request,
    promotion_id: UUID,
    body: PromotionUpdate,
):
    return await promotions_service.update_promotion(request, promotion_id, body)


@router.delete(
    "/{promotion_id}",
    dependencies=[Depends(require_module(Module.MI_NEGOCIO))],
    summary="Delete promotion",
)
async def delete_promotion_endpoint(
    request: Request,
    promotion_id: UUID,
    body: PromotionDeleteRequest,
):
    return await promotions_service.delete_promotion(
        request,
        promotion_id,
        reason=body.reason,
    )

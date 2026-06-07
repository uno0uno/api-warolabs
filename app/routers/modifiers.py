from fastapi import APIRouter, Depends, Request, Response, Query
from typing import Optional
from uuid import UUID
from app.core.permissions import Module, require_module
from app.models.modifier import (
    ModifierGroupCreate,
    ModifierGroupUpdate,
    ModifierGroupResponse,
    ModifierGroupsListResponse,
    ModifierGroupStats
)
from app.services import modifiers_service

router = APIRouter()

@router.post("", response_model=ModifierGroupResponse, dependencies=[Depends(require_module(Module.MENU))])
async def create_modifier_group(
    request: Request,
    group_data: ModifierGroupCreate
):
    """Create a new modifier group with modifiers"""
    return await modifiers_service.create_modifier_group(request, group_data)


@router.get("/{group_id}", response_model=ModifierGroupResponse, dependencies=[Depends(require_module(Module.MENU))])
async def get_modifier_group(
    request: Request,
    group_id: UUID
):
    """Get a single modifier group by ID"""
    return await modifiers_service.get_modifier_group_by_id(request, group_id)


@router.get("", response_model=ModifierGroupsListResponse, dependencies=[Depends(require_module(Module.MENU))])
async def get_modifier_groups(
    request: Request,
    response: Response,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=250),
    search: Optional[str] = Query(None),
    product_id: Optional[UUID] = Query(None),
    is_required: Optional[bool] = Query(None)
):
    """Get list of modifier groups with pagination and filters"""
    return await modifiers_service.get_modifier_groups_list(
        request, response, page, limit, search, product_id, is_required
    )


@router.get("/stats/summary", response_model=ModifierGroupStats, dependencies=[Depends(require_module(Module.MENU))])
async def get_modifier_group_stats(request: Request):
    """Get modifier group statistics"""
    return await modifiers_service.get_modifier_group_stats(request)


@router.put("/{group_id}", response_model=ModifierGroupResponse, dependencies=[Depends(require_module(Module.MENU))])
async def update_modifier_group(
    request: Request,
    group_id: UUID,
    group_data: ModifierGroupUpdate
):
    """Update a modifier group and its modifiers"""
    return await modifiers_service.update_modifier_group(request, group_id, group_data)


@router.delete("/{group_id}", dependencies=[Depends(require_module(Module.MENU))])
async def delete_modifier_group(
    request: Request,
    group_id: UUID
):
    """Delete a modifier group and its modifiers"""
    return await modifiers_service.delete_modifier_group(request, group_id)

from fastapi import APIRouter, Request, Response, Query
from typing import Optional
from uuid import UUID
from app.models.combo import (
    ComboCreate,
    ComboUpdate,
    ComboResponse,
    CombosListResponse,
    ComboStats
)
from app.services import combos_service

router = APIRouter()

@router.post("", response_model=ComboResponse)
async def create_combo(
    request: Request,
    combo_data: ComboCreate
):
    """Create a new combo with items"""
    return await combos_service.create_combo(request, combo_data)


@router.get("/{combo_id}", response_model=ComboResponse)
async def get_combo(
    request: Request,
    combo_id: UUID
):
    """Get a single combo by ID"""
    return await combos_service.get_combo_by_id(request, combo_id)


@router.get("", response_model=CombosListResponse)
async def get_combos(
    request: Request,
    response: Response,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=250),
    search: Optional[str] = Query(None),
    category_id: Optional[UUID] = Query(None),
    is_available: Optional[bool] = Query(None)
):
    """Get list of combos with pagination and filters"""
    return await combos_service.get_combos_list(
        request, response, page, limit, search, category_id, is_available
    )


@router.get("/stats/summary", response_model=ComboStats)
async def get_combo_stats(request: Request):
    """Get combo statistics"""
    return await combos_service.get_combo_stats(request)


@router.put("/{combo_id}", response_model=ComboResponse)
async def update_combo(
    request: Request,
    combo_id: UUID,
    combo_data: ComboUpdate
):
    """Update a combo and its items"""
    return await combos_service.update_combo(request, combo_id, combo_data)


@router.delete("/{combo_id}")
async def delete_combo(
    request: Request,
    combo_id: UUID
):
    """Delete a combo and its items"""
    return await combos_service.delete_combo(request, combo_id)

from fastapi import APIRouter, Request, Response, Query, Body
from typing import Optional
from uuid import UUID
from app.services.ingredients_service import get_ingredients_list, update_ingredient_unit_weight
from app.models.ingredient import IngredientsListResponse

router = APIRouter()

@router.get("", response_model=IngredientsListResponse)
async def get_ingredients_endpoint(
    request: Request,
    response: Response,
    page: int = Query(default=1, ge=1, description="Page number"),
    limit: int = Query(default=50, ge=1, le=10000, description="Items per page"),
    search: Optional[str] = Query(default=None, description="Search by name or description"),
    category: Optional[str] = Query(default=None, description="Filter by ingredient category"),
    supplier_id: Optional[UUID] = Query(default=None, description="Filter by supplier ID"),
    type: Optional[str] = Query(default=None, description="Filter by type: 'food', 'service', 'supply'"),
    is_resale: Optional[bool] = Query(default=None, description="Filter resale ingredients only")
):
    """
    Get ingredients list with tenant isolation
    Requires valid session with tenant context
    """
    return await get_ingredients_list(
        request, response, page, limit, search, category, supplier_id, type, is_resale
    )


@router.patch("/{ingredient_id}/unit-weight")
async def patch_ingredient_unit_weight(
    ingredient_id: UUID,
    request: Request,
    response: Response,
    unit_weight_gr: Optional[float] = Body(..., embed=True, ge=0, description="Weight in grams of 1 base unit")
):
    """
    Update the unit_weight_gr field of an ingredient.
    Used to define how many grams 1 base unit weighs (e.g. 1 tajada = 20gr).
    Only applies to 'und' ingredients.
    """
    return await update_ingredient_unit_weight(request, response, ingredient_id, unit_weight_gr)

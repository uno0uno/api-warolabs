from fastapi import APIRouter, Request, Response, Query
from typing import Optional
from uuid import UUID
from app.services.ingredients_service import get_ingredients_list
from app.models.ingredient import IngredientsListResponse

router = APIRouter()

@router.get("", response_model=IngredientsListResponse)
async def get_ingredients_endpoint(
    request: Request,
    response: Response,
    page: int = Query(default=1, ge=1, description="Page number"),
    limit: int = Query(default=50, ge=1, le=250, description="Items per page"),
    search: Optional[str] = Query(default=None, description="Search by name or description"),
    category: Optional[str] = Query(default=None, description="Filter by ingredient category"),
    supplier_id: Optional[UUID] = Query(default=None, description="Filter by supplier ID")
):
    """
    Get ingredients list with tenant isolation
    Requires valid session with tenant context
    """
    return await get_ingredients_list(
        request, response, page, limit, search, category, supplier_id
    )

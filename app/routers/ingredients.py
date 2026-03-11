from fastapi import APIRouter, Request, Response, Query, Body, HTTPException
from typing import Optional
from uuid import UUID
from app.services.ingredients_service import get_ingredients_list, update_ingredient_unit_weight, match_ingredient_by_name
from app.models.ingredient import IngredientsListResponse
from app.database import get_db_connection

router = APIRouter()


@router.get("/match")
async def match_ingredient(
    name: str = Query(..., min_length=1),
    threshold: float = Query(default=0.35, ge=0.1, le=1.0)
):
    """
    Find the closest ingredient by name using pg_trgm similarity.
    Used by the frontend OCR invoice flow to replace client-side fuzzy matching.
    """
    async with get_db_connection() as conn:
        match = await match_ingredient_by_name(conn, name, threshold)
    if not match:
        raise HTTPException(status_code=404, detail="No matching ingredient found")
    return {"success": True, "data": match}


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


@router.get("/{ingredient_id}")
async def get_ingredient_by_id(ingredient_id: UUID):
    """
    Fetch a single ingredient by its UUID.
    Used by the frontend OCR flow to populate the ingredient cache for
    detected_ingredient_id items without relying on name similarity.
    """
    async with get_db_connection() as conn:
        row = await conn.fetchrow(
            "SELECT id, name, unit, type, unit_weight_gr FROM ingredients WHERE id = $1",
            ingredient_id
        )
    if not row:
        raise HTTPException(status_code=404, detail="Ingredient not found")
    return {
        "success": True,
        "data": {
            "id": str(row["id"]),
            "name": row["name"],
            "unit": row["unit"],
            "type": row["type"],
            "unit_weight_gr": float(row["unit_weight_gr"]) if row["unit_weight_gr"] is not None else None,
        }
    }


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

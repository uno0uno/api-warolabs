from fastapi import APIRouter, Request, Response, Query, Body, HTTPException
from typing import Any, Dict, Optional
from uuid import UUID
from app.services.ingredients_service import get_ingredients_list, update_ingredient_unit_weight, match_ingredient_by_name, create_tenant_ingredient
from app.models.ingredient import IngredientsListResponse, TenantIngredientCreate
from app.database import get_db_connection
from app.core.middleware import require_valid_session

router = APIRouter()


@router.post("", status_code=201)
async def create_custom_ingredient(
    body: TenantIngredientCreate,
    request: Request,
) -> Dict[str, Any]:
    """
    Create a tenant-scoped custom ingredient.

    The ingredient is only visible to the requesting restaurant.
    Optionally link to a global base ingredient via parent_id.
    """
    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id

    if not tenant_id:
        raise HTTPException(status_code=401, detail="Tenant context required")

    async with get_db_connection() as conn:
        data = await create_tenant_ingredient(conn, tenant_id, body)

    return {"success": True, "data": data}


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
    is_resale: Optional[bool] = Query(default=None, description="Filter resale ingredients only"),
    base_only: Optional[bool] = Query(default=None, description="When true, exclude variant ingredients (those with a base assigned)"),
):
    """
    Get ingredients list with tenant isolation
    Requires valid session with tenant context
    """
    return await get_ingredients_list(
        request, response, page, limit, search, category, supplier_id, type, is_resale, base_only
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


@router.get("/{ingredient_id}/variants")
async def list_ingredient_variants(
    ingredient_id: UUID,
    request: Request,
    response: Response,
) -> Dict[str, Any]:
    """
    List all variants of a base ingredient for the tenant catalog view.
    Uses ingredient_global_hierarchy — accessible to any authenticated session.
    """
    require_valid_session(request)

    async with get_db_connection() as conn:
        base = await conn.fetchrow(
            "SELECT id::text, name, unit FROM ingredients WHERE id = $1 AND tenant_id IS NULL",
            ingredient_id,
        )
        if not base:
            raise HTTPException(status_code=404, detail="Ingredient not found")

        rows = await conn.fetch(
            """
            SELECT i.id::text, i.name, i.unit, i.category, i.type
            FROM ingredients i
            JOIN ingredient_global_hierarchy h ON h.variant_id = i.id
            WHERE h.base_id = $1
            ORDER BY i.name
            """,
            ingredient_id,
        )

    return {
        "success": True,
        "base": dict(base),
        "data": [dict(r) for r in rows],
        "count": len(rows),
    }

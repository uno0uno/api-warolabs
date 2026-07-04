from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from app.core.permissions import Module, require_module
from app.models.product import ProductsListResponse
from app.services.products_service import get_products_list


router = APIRouter(prefix="/pos/products", tags=["pos"])


@router.get("", response_model=ProductsListResponse, dependencies=[Depends(require_module(Module.POS))])
async def get_pos_products_endpoint(
    request: Request,
    response: Response,
    page: int = Query(default=1, ge=1, description="Page number"),
    limit: int = Query(default=50, ge=1, le=250, description="Items per page"),
    search: Optional[str] = Query(default=None, description="Search term (see search_field)"),
    search_field: Optional[str] = Query(
        default=None,
        description="Search in a single field: name, description, or kitchen_name. Omit to search name OR description.",
    ),
    category_id: Optional[UUID] = Query(default=None, description="Filter by category ID"),
    is_available: Optional[bool] = Query(default=None, description="Filter by availability"),
    is_combo: Optional[bool] = Query(default=None, description="Filter combos only"),
    is_resale: Optional[bool] = Query(default=None, description="Filter resale products only"),
    station_id: Optional[UUID] = Query(
        default=None,
        description="Filter by kitchen station (product.station_id OR category default station)",
    ),
    is_available_online: Optional[bool] = Query(default=None, description="Filter by online menu availability"),
    is_available_table_qr: Optional[bool] = Query(default=None, description="Filter by table QR menu availability"),
    has_recipe: Optional[bool] = Query(
        default=None,
        description="Filter products with/without recipe (ingredients, recipe bases, or base type)",
    ),
    margin_negative: Optional[bool] = Query(
        default=None,
        description="When true, only products where calculated cost exceeds price",
    ),
    sort: Optional[str] = Query(
        default=None,
        description=(
            "Sort order: created_at_desc (default), created_at_asc, name_asc, name_desc, "
            "price_asc, price_desc, margin_asc, margin_desc"
        ),
    ),
    include_ingredients: bool = Query(default=False, description="Include recipe ingredients in response"),
    include_modifiers: bool = Query(default=False, description="Include modifier groups in response (for POS)"),
    include_all_types: bool = Query(
        default=False,
        description="Include menu and resale products (Productos list Todos filter)",
    ),
):
    """POS-scoped product catalog read endpoint."""
    return await get_products_list(
        request,
        response,
        page,
        limit,
        search,
        search_field,
        category_id,
        is_available,
        is_combo,
        is_resale,
        station_id,
        is_available_online,
        is_available_table_qr,
        has_recipe,
        margin_negative,
        sort,
        include_ingredients,
        include_modifiers,
        include_all_types,
    )

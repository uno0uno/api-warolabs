from fastapi import APIRouter, Request, Response, Query, Body
from typing import Optional
from uuid import UUID
from app.services.products_service import (
    create_product_with_recipe,
    get_product_by_id,
    get_products_list,
    get_product_stats,
    update_product_with_recipe,
    delete_product
)
from app.models.product import (
    ProductCreate,
    ProductUpdate,
    ProductResponse,
    ProductsListResponse,
    ProductStats
)

router = APIRouter()

@router.post("", response_model=ProductResponse)
async def create_product_endpoint(
    request: Request,
    product_data: ProductCreate = Body(...)
):
    """
    Create a product with its recipe in a single transaction.

    The product will be created with:
    - Basic product information (name, price, category, etc.)
    - Recipe ingredients (list of ingredient_id, quantity, unit)
    - Automatically calculated cost based on ingredients
    - Calculated margin (percentage and value)

    Requires valid session with tenant context.
    """
    return await create_product_with_recipe(request, product_data)


@router.get("", response_model=ProductsListResponse)
async def get_products_endpoint(
    request: Request,
    response: Response,
    page: int = Query(default=1, ge=1, description="Page number"),
    limit: int = Query(default=50, ge=1, le=250, description="Items per page"),
    search: Optional[str] = Query(default=None, description="Search by name or description"),
    category_id: Optional[UUID] = Query(default=None, description="Filter by category ID"),
    is_available: Optional[bool] = Query(default=None, description="Filter by availability"),
    is_combo: Optional[bool] = Query(default=None, description="Filter combos only"),
    include_ingredients: bool = Query(default=False, description="Include recipe ingredients in response")
):
    """
    Get products list with filters and pagination.

    Returns products with:
    - Basic product information
    - Category name
    - Calculated cost and margins
    - Optionally recipe details (set include_ingredients=true)

    Requires valid session with tenant context.
    """
    return await get_products_list(
        request, response, page, limit, search, category_id, is_available, is_combo, include_ingredients
    )


@router.get("/stats", response_model=ProductStats)
async def get_products_stats_endpoint(request: Request):
    """
    Get product statistics for dashboard.

    Returns:
    - Total products
    - Available products
    - Products with stock control
    - Combo products
    - Average margin percentage

    Requires valid session with tenant context.
    """
    return await get_product_stats(request)


@router.get("/{product_id}", response_model=ProductResponse)
async def get_product_endpoint(
    request: Request,
    product_id: UUID
):
    """
    Get a single product with full details including recipe.

    Returns complete product with:
    - All product fields
    - Category information
    - Complete recipe with ingredients
    - Calculated cost and margins

    Requires valid session with tenant context.
    """
    return await get_product_by_id(request, product_id)


@router.put("/{product_id}", response_model=ProductResponse)
async def update_product_endpoint(
    request: Request,
    product_id: UUID,
    product_data: ProductUpdate = Body(...)
):
    """
    Update a product with its recipe in a single transaction.

    Can update:
    - Product fields (name, price, category, etc.)
    - Recipe ingredients (will replace existing recipe if provided)
    - Automatically recalculates cost based on new ingredients

    Requires valid session with tenant context.
    """
    return await update_product_with_recipe(request, product_id, product_data)


@router.delete("/{product_id}")
async def delete_product_endpoint(
    request: Request,
    product_id: UUID
):
    """
    Delete a product and its recipe.

    This will permanently delete the product and all associated recipe data.

    Requires valid session with tenant context.
    """
    return await delete_product(request, product_id)

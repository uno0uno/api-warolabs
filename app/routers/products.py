from fastapi import APIRouter, HTTPException, Request, Response, Query, Body
from fastapi.responses import JSONResponse
from typing import Optional
from uuid import UUID
from app.core.exceptions import AuthenticationError
from app.services.products_service import (
    create_product_with_recipe,
    get_product_by_id,
    get_products_list,
    get_product_stats,
    update_product_with_recipe,
    delete_product,
    upload_product_image,
)
from app.models.product import (
    ProductCreate,
    ProductUpdate,
    ProductResponse,
    ProductsListResponse,
    ProductStats
)

# Mirrors tenant_config.py constants for cross-router consistency.
ALLOWED_IMAGE_TYPES = {'image/jpeg', 'image/png', 'image/webp'}
MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5MB

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
    is_resale: Optional[bool] = Query(default=None, description="Filter resale products only"),
    include_ingredients: bool = Query(default=False, description="Include recipe ingredients in response"),
    include_modifiers: bool = Query(default=False, description="Include modifier groups in response (for POS)")
):
    """
    Get products list with filters and pagination.

    Returns products with:
    - Basic product information
    - Category name
    - Calculated cost and margins
    - Optionally recipe details (set include_ingredients=true)
    - Optionally modifier groups (set include_modifiers=true for POS)

    Requires valid session with tenant context.
    """
    return await get_products_list(
        request, response, page, limit, search, category_id, is_available, is_combo, is_resale, include_ingredients, include_modifiers
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


@router.post("/upload-image")
async def upload_product_image_endpoint(request: Request):
    """Upload a product hero image to Cloudflare R2 (issue #465).

    Accepts multipart/form-data with:
      - file: image (JPEG, PNG or WebP, max 5MB)

    Returns: { "url": "https://pub-….r2.dev/product-images/{tenant_id}/{uuid}.{ext}" }

    The URL is permanent. The frontend stores it in `form.image_url` and the
    submit of the product persists it as `product.image_url`.

    Requires valid session with tenant context.
    """
    try:
        form = await request.form()
        file = form.get("file")

        if not file or not hasattr(file, 'read'):
            raise HTTPException(status_code=400, detail="No file provided")

        content_type = getattr(file, 'content_type', None) or 'application/octet-stream'
        if content_type not in ALLOWED_IMAGE_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"File type not allowed. Use JPEG, PNG, or WebP. Got: {content_type}",
            )

        file_bytes = await file.read()

        if len(file_bytes) > MAX_IMAGE_SIZE:
            raise HTTPException(status_code=400, detail="File too large. Maximum size is 5MB.")

        public_url = await upload_product_image(
            request=request,
            file_bytes=file_bytes,
            filename=getattr(file, 'filename', 'image.jpg') or 'image.jpg',
            content_type=content_type,
        )

        return JSONResponse({"url": public_url})

    except HTTPException:
        raise
    except AuthenticationError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")

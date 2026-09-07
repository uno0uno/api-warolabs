from fastapi import APIRouter, Depends, HTTPException, Request, Response, Query, Body
from fastapi.responses import JSONResponse
from typing import Optional
from uuid import UUID
from app.core.exceptions import AuthenticationError
from app.core.permissions import Module, require_module
from app.services.products_service import (
    create_product_with_recipe,
    convert_product_to_resale,
    get_product_by_id,
    get_products_list,
    get_product_stats,
    update_product_with_recipe,
    delete_product,
    upload_product_image,
)
from app.models.product import (
    ProductCreate,
    ProductConvertToResale,
    ProductUpdate,
    ProductResponse,
    ProductsListResponse,
    ProductStats
)

# Mirrors tenant_config.py constants for cross-router consistency.
ALLOWED_IMAGE_TYPES = {'image/jpeg', 'image/png', 'image/webp'}
MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5MB

router = APIRouter()

@router.post("", response_model=ProductResponse, dependencies=[Depends(require_module(Module.MENU))])
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

    **Atomic resale** (`auto_resale_ingredient=true`): requires `is_resale=true`, empty
    recipe arrays, and `resale_unit_weight_gr`. Creates tenant ingredient (und, is_resale)
    + product + one `product_recipes` row in the same transaction. Response includes
    `resale_ingredient_id` (also in `ingredients[0].ingredient_id`).

    **Duplicate names**: ingredient and product names are unique per tenant in separate
    indexes — the same display name on both rows is allowed. Ingredient duplicate → 409
    with ingredient message; product duplicate → 409 with product message (txn rolls back).

    Requires valid session with tenant context.
    """
    return await create_product_with_recipe(request, product_data)


@router.get("", response_model=ProductsListResponse, dependencies=[Depends(require_module(Module.MENU))])
async def get_products_endpoint(
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


@router.get("/stats", response_model=ProductStats, dependencies=[Depends(require_module(Module.MENU))])
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


@router.get("/{product_id}", response_model=ProductResponse, dependencies=[Depends(require_module(Module.MENU))])
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


@router.post(
    "/{product_id}/convert-to-resale",
    response_model=ProductResponse,
    dependencies=[Depends(require_module(Module.MENU))],
)
async def convert_product_to_resale_endpoint(
    request: Request,
    product_id: UUID,
    body: ProductConvertToResale = Body(...),
):
    """
    Convert a menu product without recipe into atomic resale (1 sale = 1 und).

    Requires: not already resale, no recipe rows, not combo/open-priced, no modifier groups.
    Atomically creates linked tenant ingredient + product_recipes row and sets is_resale=true.
    """
    return await convert_product_to_resale(request, product_id, body)


@router.put("/{product_id}", response_model=ProductResponse, dependencies=[Depends(require_module(Module.MENU))])
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


@router.delete("/{product_id}", dependencies=[Depends(require_module(Module.MENU))])
async def delete_product_endpoint(
    request: Request,
    product_id: UUID,
    payload: dict = Body(default={}),
):
    """
    Delete or archive a product. Requires `reason` in body (Bitácora audit).

    Products with sales history (order_items) are archived (is_available=false) to
    preserve orders and KDS links. Products never sold are permanently deleted.

    Requires valid session with tenant context.
    """
    reason = (payload or {}).get("reason", "").strip() if isinstance(payload, dict) else ""
    if not reason:
        raise HTTPException(status_code=422, detail="reason is required")
    return await delete_product(request, product_id, reason=reason)


@router.post("/upload-image", dependencies=[Depends(require_module(Module.MENU))])
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

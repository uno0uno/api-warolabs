"""
Public restaurant router - public endpoints for restaurant profiles and menus
No authentication required
"""
from fastapi import APIRouter, HTTPException, Query, Response
from typing import Optional, Dict, Any
from uuid import UUID
from app.core.exceptions import NotFoundError
from app.services import payment_method_service, public_restaurant_service

import logging

logger = logging.getLogger(__name__)

router = APIRouter()


def _mark_dynamic_public_response(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"


@router.get("/list")
async def list_public_restaurants(
    response: Response,
    city: Optional[str] = Query(
        default=None,
        description="Deprecated: filter by display-name city. Use city_slug instead."
    ),
    city_slug: Optional[str] = Query(
        default=None,
        description="Preferred: filter by normalized slug (e.g. 'bogota', 'mosquera')."
    ),
) -> Dict[str, Any]:
    """
    List all active public restaurant profiles.

    Optional filters:
    - city_slug: preferred (matches `tenant_public_profiles.city_slug`)
    - city: deprecated alias kept for one release; logs a warning when used.

    Returns list of restaurants with basic info plus `country` and `city_slug`.

    **Public endpoint — no authentication required**

    Example: GET /api/public/restaurant/list
    Example: GET /api/public/restaurant/list?city_slug=bogota
    """
    logger.info(
        "🔍 [list_public_restaurants] Request city=%r city_slug=%r",
        city, city_slug,
    )
    _mark_dynamic_public_response(response)

    restaurants = await public_restaurant_service.list_restaurants(
        city=city, city_slug=city_slug,
    )

    logger.info(f"🔍 [list_public_restaurants] Found {len(restaurants)} restaurants")

    return {
        "success": True,
        "data": restaurants
    }


@router.get("/cities")
async def list_public_cities(
    include_empty: bool = Query(
        default=False,
        description="Include catalog entries with zero active tenants. "
                    "True for the operator selector on /negocio; "
                    "False (default) for the discovery section on /."
    ),
) -> Dict[str, Any]:
    """
    Return the city/municipality catalog (warocol.com#615, #1477).

    Public — no auth required. Used by the operator-facing city selector
    on /negocio and the customer-facing discovery section on the root
    landing page. Includes DIVIPOLA metadata when available.
    """
    cities = await public_restaurant_service.list_cities(
        include_empty=include_empty,
    )
    return {"success": True, "data": cities}


@router.get("/{tenant_slug}/payment-methods")
async def list_public_payment_methods(response: Response, tenant_slug: str) -> Dict[str, Any]:
    """
    Active payment groups + nested methods that the customer can pick at
    online checkout. Public — no auth header required (warocol.com#610).

    Mirrors the shape of `/pos/payment-methods` and `/online/orders/payment-methods`
    but resolves the tenant by `tenant_slug` and excludes `triggersCartera=true`
    groups (anonymous customers cannot accrue cartera).
    """
    _mark_dynamic_public_response(response)
    try:
        return await payment_method_service.list_public_methods_by_tenant_slug(
            tenant_slug
        )
    except NotFoundError:
        raise HTTPException(
            status_code=404,
            detail="Restaurant not found or not active",
        )


@router.get("/{tenant_slug}")
async def get_public_profile(
    response: Response,
    tenant_slug: str
) -> Dict[str, Any]:
    logger.info(f"🔍 [get_public_profile] Request for slug: {tenant_slug}")
    _mark_dynamic_public_response(response)
    """
    Get public restaurant profile by slug

    Returns restaurant information:
    - Basic info (name, description, logo, banner)
    - Contact info (phone, email, address)
    - Location (city, neighborhood, coordinates)
    - Business hours
    - Social media links
    - SEO metadata
    - is_currently_open (calculated)

    **Public endpoint - no authentication required**

    Example: GET /api/public/restaurant/la-hamburgueseria
    """
    profile = await public_restaurant_service.get_profile_by_slug(tenant_slug)

    logger.info(f"🔍 [get_public_profile] Service result for {tenant_slug}: {profile is not None}")


    if not profile:
        raise HTTPException(
            status_code=404,
            detail="Restaurant not found or not active"
        )

    return {
        "success": True,
        "data": profile
    }


@router.get("/{tenant_slug}/menu")
async def get_public_menu(
    response: Response,
    tenant_slug: str,
    category_id: Optional[UUID] = Query(
        default=None,
        description="Optional: filter by category ID"
    )
) -> Dict[str, Any]:
    logger.info(f"🔍 [get_public_menu] Request for slug: {tenant_slug}, category: {category_id}")
    _mark_dynamic_public_response(response)
    """
    Get public menu for a restaurant

    Returns:
    - restaurant_name: Display name of the restaurant
    - categories: List of available categories
    - products: List of products with:
      - id, name, description, price
      - category_id, category_name
      - is_available, preparation_time
      - has_modifiers (boolean)

    **Public endpoint - no authentication required**

    Example: GET /api/public/restaurant/la-hamburgueseria/menu
    Example with filter: GET /api/public/restaurant/la-hamburgueseria/menu?category_id=...
    """
    menu = await public_restaurant_service.get_menu_by_slug(
        tenant_slug,
        category_id=category_id
    )
    logger.info(f"🔍 [get_public_menu] Service result for {tenant_slug}: {'Found' if menu else 'None'}")

    return {
        "success": True,
        "data": menu
    }


@router.get("/{tenant_slug}/product/{product_id}")
async def get_public_product_detail(
    response: Response,
    tenant_slug: str,
    product_id: UUID
) -> Dict[str, Any]:
    """
    Get detailed product information with modifiers

    Returns product details:
    - id, name, description, price
    - category_name
    - is_available, preparation_time
    - modifier_groups: Array of modifier groups with their modifiers
      - Each group has: id, name, is_required, min_qty, max_qty
      - Each modifier has: id, name, price, is_available

    **Public endpoint - no authentication required**

    Example: GET /api/public/restaurant/la-hamburgueseria/product/uuid-here
    """
    _mark_dynamic_public_response(response)
    product = await public_restaurant_service.get_product_detail(
        tenant_slug,
        product_id
    )

    return {
        "success": True,
        "data": product
    }

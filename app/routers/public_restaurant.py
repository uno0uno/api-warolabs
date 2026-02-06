"""
Public restaurant router - public endpoints for restaurant profiles and menus
No authentication required
"""
from fastapi import APIRouter, HTTPException, Query
from typing import Optional, Dict, Any
from uuid import UUID
from app.services import public_restaurant_service

import logging

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/list")
async def list_public_restaurants(
    city: Optional[str] = Query(
        default=None,
        description="Optional: filter by city (e.g., 'Bogotá')"
    )
) -> Dict[str, Any]:
    """
    List all active public restaurant profiles

    Optional filters:
    - city: Filter by city name

    Returns list of restaurants with basic info:
    - id, tenant_id, slug
    - display_name, description
    - logo_url, banner_url
    - city, address
    - phone_number, email
    - is_active

    **Public endpoint - no authentication required**

    Example: GET /api/public/restaurant/list
    Example with filter: GET /api/public/restaurant/list?city=Bogotá
    """
    logger.info(f"🔍 [list_public_restaurants] Request with city filter: {city}")

    restaurants = await public_restaurant_service.list_restaurants(city=city)

    logger.info(f"🔍 [list_public_restaurants] Found {len(restaurants)} restaurants")

    return {
        "success": True,
        "data": restaurants
    }


@router.get("/{tenant_slug}")
async def get_public_profile(
    tenant_slug: str
) -> Dict[str, Any]:
    logger.info(f"🔍 [get_public_profile] Request for slug: {tenant_slug}")
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
    tenant_slug: str,
    category_id: Optional[UUID] = Query(
        default=None,
        description="Optional: filter by category ID"
    )
) -> Dict[str, Any]:
    logger.info(f"🔍 [get_public_menu] Request for slug: {tenant_slug}, category: {category_id}")
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
    product = await public_restaurant_service.get_product_detail(
        tenant_slug,
        product_id
    )

    return {
        "success": True,
        "data": product
    }

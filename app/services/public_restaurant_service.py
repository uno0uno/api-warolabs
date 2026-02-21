"""
Public restaurant service - handles public profile and menu queries
No authentication required - these are public endpoints
"""
from typing import Optional, Dict, Any, List
from uuid import UUID
from datetime import datetime, time
from fastapi import HTTPException
from app.database import get_db_connection
from app.models.tenant_public_profile import (
    TenantPublicProfile, TenantPublicProfileResponse
)
from app.models.product import Product
from decimal import Decimal
import json
import logging

logger = logging.getLogger(__name__)


async def get_profile_by_slug(slug: str) -> Optional[Dict[str, Any]]:
    """
    Get public restaurant profile by slug

    Args:
        slug: URL-friendly slug (e.g., 'la-hamburgueseria')

    Returns:
        Profile data with calculated 'is_currently_open' field, or None if not found/inactive
    """
    try:
        async with get_db_connection() as conn:
            query = """
                SELECT
                    id, tenant_id, slug, is_active,
                    display_name, description, logo_url, banner_url,
                    phone_number, email, address,
                    city, neighborhood, latitude, longitude,
                    business_hours, social_media,
                    seo_title, seo_description,
                    accepts_online_orders, min_order_amount, estimated_preparation_time,
                    created_at, updated_at
                FROM tenant_public_profiles
                WHERE slug = $1 AND is_active = true
            """

            row = await conn.fetchrow(query, slug)

            if not row:
                return None

            # Convert to dict
            profile = dict(row)

            # Parse JSONB fields if they're strings
            if isinstance(profile.get('business_hours'), str):
                try:
                    profile['business_hours'] = json.loads(profile['business_hours'])
                except (json.JSONDecodeError, TypeError):
                    profile['business_hours'] = {}

            if isinstance(profile.get('social_media'), str):
                try:
                    profile['social_media'] = json.loads(profile['social_media'])
                except (json.JSONDecodeError, TypeError):
                    profile['social_media'] = {}

            # Calculate if currently open
            profile['is_currently_open'] = is_currently_open(profile.get('business_hours'))

            return profile

    except Exception as e:
        logger.error(f"Error getting profile by slug '{slug}': {e}")
        raise HTTPException(status_code=500, detail="Error fetching restaurant profile")


async def get_menu_by_slug(
    slug: str,
    category_id: Optional[UUID] = None
) -> Dict[str, Any]:
    """
    Get public menu for a restaurant by slug

    Args:
        slug: Restaurant slug
        category_id: Optional filter by category

    Returns:
        {
            "restaurant_name": str,
            "categories": List[{id, name, description}],
            "products": List[Product]
        }
    """
    try:
        async with get_db_connection() as conn:
            # 1. Get tenant_id from profile
            profile_query = """
                SELECT tenant_id, display_name
                FROM tenant_public_profiles
                WHERE slug = $1 AND is_active = true
            """
            profile = await conn.fetchrow(profile_query, slug)

            if not profile:
                raise HTTPException(
                    status_code=404,
                    detail="Restaurant not found or not active"
                )

            tenant_id = profile['tenant_id']
            restaurant_name = profile['display_name']

            # 2. Get categories for this tenant
            categories_query = """
                SELECT DISTINCT c.id, c.name, c.description
                FROM categories c
                JOIN product p ON p.category_id = c.id
                WHERE p.tenant_id = $1 AND p.is_available = true
                ORDER BY c.name
            """
            categories_rows = await conn.fetch(categories_query, tenant_id)
            categories = [dict(row) for row in categories_rows]

            # 3. Get products
            products_query = """
                SELECT
                    p.id, p.name, p.description, p.price,
                    p.category_id, c.name as category_name,
                    p.is_available, p.preparation_time,
                    p.allow_modifiers,
                    EXISTS(
                        SELECT 1
                        FROM product_modifier_groups pmg
                        JOIN modifiers m ON m.modifier_group_id = pmg.modifier_group_id
                        WHERE pmg.product_id = p.id
                          AND m.is_available = true
                    ) as has_modifiers
                FROM product p
                JOIN categories c ON p.category_id = c.id
                WHERE p.tenant_id = $1 AND p.is_available = true
            """

            params = [tenant_id]

            # Add category filter if provided
            if category_id:
                products_query += " AND p.category_id = $2"
                params.append(category_id)

            products_query += " ORDER BY c.name, p.name"

            products_rows = await conn.fetch(products_query, *params)
            products = [dict(row) for row in products_rows]
            for p in products:
                p['price'] = float(p['price'])

            return {
                "restaurant_name": restaurant_name,
                "categories": categories,
                "products": products
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting menu for slug '{slug}': {e}")
        raise HTTPException(status_code=500, detail="Error fetching menu")


async def get_product_detail(slug: str, product_id: UUID) -> Dict[str, Any]:
    """
    Get detailed product information with modifiers

    Args:
        slug: Restaurant slug
        product_id: Product ID

    Returns:
        Product details with modifier_groups array
    """
    try:
        async with get_db_connection() as conn:
            # 1. Verify tenant owns this product
            verification_query = """
                SELECT p.id
                FROM product p
                JOIN tenant_public_profiles tpp ON tpp.tenant_id = p.tenant_id
                WHERE tpp.slug = $1 AND tpp.is_active = true AND p.id = $2
            """
            verification = await conn.fetchrow(verification_query, slug, product_id)

            if not verification:
                raise HTTPException(
                    status_code=404,
                    detail="Product not found for this restaurant"
                )

            # 2. Get product details
            product_query = """
                SELECT
                    p.id, p.name, p.description, p.price,
                    c.name as category_name,
                    p.is_available, p.preparation_time
                FROM product p
                JOIN categories c ON p.category_id = c.id
                WHERE p.id = $1
            """
            product_row = await conn.fetchrow(product_query, product_id)

            if not product_row:
                raise HTTPException(status_code=404, detail="Product not found")

            product = dict(product_row)
            product['price'] = float(product['price'])

            # 3. Get modifier groups and modifiers via junction table
            modifiers_query = """
                SELECT
                    mg.id as group_id,
                    mg.name as group_name,
                    mg.is_required,
                    mg.min_qty,
                    mg.max_qty,
                    mg.sort_order as group_sort_order,
                    m.id as modifier_id,
                    m.name as modifier_name,
                    m.price as modifier_price,
                    m.is_available as modifier_is_available,
                    m.is_default as modifier_is_default,
                    m.max_limit as modifier_max_limit,
                    m.sort_order as modifier_sort_order
                FROM product_modifier_groups pmg
                JOIN modifier_groups mg ON mg.id = pmg.modifier_group_id
                LEFT JOIN modifiers m ON m.modifier_group_id = mg.id
                WHERE pmg.product_id = $1
                ORDER BY mg.sort_order, m.sort_order
            """
            modifiers_rows = await conn.fetch(modifiers_query, product_id)

            # Organize modifiers into groups
            modifier_groups = {}
            for row in modifiers_rows:
                group_id = str(row['group_id'])

                if group_id not in modifier_groups:
                    modifier_groups[group_id] = {
                        'id': row['group_id'],
                        'name': row['group_name'],
                        'is_required': row['is_required'],
                        'min_qty': row['min_qty'],
                        'max_qty': row['max_qty'],
                        'modifiers': []
                    }

                # Only add modifier if it exists and is available
                if row['modifier_id'] and row['modifier_is_available']:
                    modifier_groups[group_id]['modifiers'].append({
                        'id': row['modifier_id'],
                        'name': row['modifier_name'],
                        'price': float(row['modifier_price']),
                        'is_available': row['modifier_is_available'],
                        'is_default': row['modifier_is_default'],
                        'max_limit': row['modifier_max_limit']
                    })

            product['modifier_groups'] = list(modifier_groups.values())

            return product

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting product detail for product {product_id}: {e}")
        raise HTTPException(status_code=500, detail="Error fetching product details")


def is_currently_open(business_hours: Optional[Dict[str, Any]]) -> bool:
    """
    Calculate if restaurant is currently open based on business hours

    Args:
        business_hours: Dict with format {
            "monday": {"open": "09:00", "close": "22:00", "closed": false},
            ...
        }

    Returns:
        True if currently open, False otherwise
    """
    if not business_hours:
        return False

    try:
        now = datetime.now()
        current_day = now.strftime('%A').lower()  # 'monday', 'tuesday', etc.
        current_time = now.time()

        day_schedule = business_hours.get(current_day)

        if not day_schedule:
            return False

        # Check if closed on this day
        if day_schedule.get('closed', False):
            return False

        # Parse opening and closing times
        open_time_str = day_schedule.get('open')
        close_time_str = day_schedule.get('close')

        if not open_time_str or not close_time_str:
            return False

        # Convert to time objects
        open_time = time.fromisoformat(open_time_str)
        close_time = time.fromisoformat(close_time_str)

        # Check if current time is within business hours
        # Handle case where closing time is after midnight
        if close_time < open_time:
            # Open overnight (e.g., 22:00 - 02:00)
            return current_time >= open_time or current_time <= close_time
        else:
            # Normal case
            return open_time <= current_time <= close_time

    except Exception as e:
        logger.error(f"Error calculating if restaurant is open: {e}")
        return False


async def validate_slug_available(slug: str, exclude_tenant_id: Optional[UUID] = None) -> bool:
    """
    Check if a slug is available for use

    Args:
        slug: Slug to validate
        exclude_tenant_id: Optional tenant ID to exclude (for updates)

    Returns:
        True if available, False if already taken
    """
    try:
        async with get_db_connection() as conn:
            query = "SELECT id FROM tenant_public_profiles WHERE slug = $1"
            params = [slug]

            if exclude_tenant_id:
                query += " AND tenant_id != $2"
                params.append(exclude_tenant_id)

            result = await conn.fetchrow(query, *params)

            return result is None  # Available if not found

    except Exception as e:
        logger.error(f"Error validating slug '{slug}': {e}")
        return False


async def list_restaurants(city: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    List all active public restaurant profiles

    Args:
        city: Optional city filter (e.g., 'Bogotá')

    Returns:
        List of restaurant profiles with basic information
    """
    try:
        async with get_db_connection() as conn:
            query = """
                SELECT
                    id, tenant_id, slug, is_active,
                    display_name, description, logo_url, banner_url,
                    phone_number, email, address,
                    city, neighborhood,
                    created_at, updated_at
                FROM tenant_public_profiles
                WHERE is_active = true
            """
            params = []

            # Add city filter if provided
            if city:
                query += " AND city = $1"
                params.append(city)

            query += " ORDER BY display_name ASC"

            rows = await conn.fetch(query, *params)

            restaurants = []
            for row in rows:
                restaurants.append({
                    "id": str(row["id"]),
                    "tenant_id": str(row["tenant_id"]),
                    "slug": row["slug"],
                    "is_active": row["is_active"],
                    "display_name": row["display_name"],
                    "description": row["description"],
                    "logo_url": row["logo_url"],
                    "banner_url": row["banner_url"],
                    "phone_number": row["phone_number"],
                    "email": row["email"],
                    "address": row["address"],
                    "city": row["city"],
                    "neighborhood": row["neighborhood"],
                    "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                    "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None
                })

            return restaurants

    except Exception as e:
        logger.error(f"Error listing restaurants (city={city}): {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Error listing restaurants: {str(e)}"
        )

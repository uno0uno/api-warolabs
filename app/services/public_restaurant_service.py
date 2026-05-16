"""
Public restaurant service - handles public profile and menu queries
No authentication required - these are public endpoints
"""
from typing import Optional, Dict, Any, List
from uuid import UUID
from datetime import datetime, time
from zoneinfo import ZoneInfo
from fastapi import HTTPException
from app.database import get_db_connection
import json
import logging

BOGOTA_TZ = ZoneInfo("America/Bogota")

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
            # JOIN tenant_subscriptions to gate out tenants without an active
            # paid subscription. INNER JOIN drops tenants with no row at all
            # (the "free / never paid" case). Visible statuses follow the
            # canonical predicate used elsewhere in billing_service: 'active'
            # plus 'past_due' (any age — only flips dark when billing actually
            # marks the row as 'cancelled').
            query = """
                SELECT
                    tpp.id, tpp.tenant_id, tpp.slug, tpp.is_active,
                    tpp.display_name, tpp.description, tpp.logo_url, tpp.banner_url,
                    tpp.phone_number, tpp.email, tpp.address,
                    tpp.city, tpp.neighborhood, tpp.latitude, tpp.longitude,
                    tpp.business_hours, tpp.social_media,
                    tpp.seo_title, tpp.seo_description,
                    tpp.accepts_online_orders, tpp.min_order_amount, tpp.estimated_preparation_time,
                    tpp.is_manually_open,
                    tpp.tip_enabled, tpp.tip_default_percentages, tpp.tip_preselect_index,
                    tpp.created_at, tpp.updated_at
                FROM tenant_public_profiles tpp
                JOIN tenant_subscriptions ts ON ts.tenant_id = tpp.tenant_id
                WHERE tpp.slug = $1
                  AND tpp.is_active = true
                  AND ts.status IN ('active', 'past_due')
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

            # warocol.com#639 — tip presets surface to the public storefront so the
            # online checkout can render the tip selector. asyncpg returns
            # numeric(5,2)[] as list[Decimal]; cast to list[float] for JSON.
            # Default to [10.0] when missing so the checkout has something to show
            # if a tenant enables tipping without explicitly setting presets.
            tip_presets = profile.get('tip_default_percentages')
            profile['tip_default_percentages'] = (
                [float(p) for p in tip_presets] if tip_presets else [10.0]
            )
            profile['tip_enabled'] = bool(profile.get('tip_enabled'))

            # Calculate if currently open — manual toggle takes priority
            profile['is_currently_open'] = is_currently_open(
                profile.get('business_hours'),
                profile.get('is_manually_open', True)
            )

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
            # 1. Get tenant_id from profile (gated by billing subscription —
            # mirror the predicate from get_profile_by_slug so a hidden tenant
            # cannot leak menu data either).
            profile_query = """
                SELECT tpp.tenant_id, tpp.display_name
                FROM tenant_public_profiles tpp
                JOIN tenant_subscriptions ts ON ts.tenant_id = tpp.tenant_id
                WHERE tpp.slug = $1
                  AND tpp.is_active = true
                  AND ts.status IN ('active', 'past_due')
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
                WHERE p.tenant_id = $1 AND p.is_available = true AND p.is_available_online = true
                ORDER BY c.name
            """
            categories_rows = await conn.fetch(categories_query, tenant_id)
            categories = [dict(row) for row in categories_rows]

            # 3. Get products
            products_query = """
                SELECT
                    p.id, p.name, p.description, p.price, p.image_url,
                    p.category_id, c.name as category_name,
                    p.is_available, p.preparation_time,
                    p.allow_modifiers,
                    EXISTS(
                        SELECT 1
                        FROM product_modifier_groups pmg
                        WHERE pmg.product_id = p.id
                    ) as has_modifiers
                FROM product p
                JOIN categories c ON p.category_id = c.id
                WHERE p.tenant_id = $1 AND p.is_available = true AND p.is_available_online = true
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


async def get_profile_by_tenant_id(tenant_id: UUID) -> Optional[Dict[str, Any]]:
    """
    Get public restaurant profile by tenant_id (used by API key authenticated endpoints)
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
                    is_manually_open,
                    created_at, updated_at
                FROM tenant_public_profiles
                WHERE tenant_id = $1 AND is_active = true
            """
            row = await conn.fetchrow(query, tenant_id)

            if not row:
                return None

            profile = dict(row)

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

            profile['is_currently_open'] = is_currently_open(
                profile.get('business_hours'),
                profile.get('is_manually_open', True)
            )

            return profile

    except Exception as e:
        logger.error(f"Error getting profile by tenant_id '{tenant_id}': {e}")
        raise HTTPException(status_code=500, detail="Error fetching restaurant profile")


async def get_menu_by_tenant_id(
    tenant_id: UUID,
    category_id: Optional[UUID] = None
) -> Dict[str, Any]:
    """
    Get public menu for a restaurant by tenant_id (used by API key authenticated endpoints)
    """
    try:
        async with get_db_connection() as conn:
            profile_query = """
                SELECT display_name
                FROM tenant_public_profiles
                WHERE tenant_id = $1 AND is_active = true
            """
            profile = await conn.fetchrow(profile_query, tenant_id)

            if not profile:
                raise HTTPException(status_code=404, detail="Restaurant not found or not active")

            restaurant_name = profile['display_name']

            categories_query = """
                SELECT DISTINCT c.id, c.name, c.description
                FROM categories c
                JOIN product p ON p.category_id = c.id
                WHERE p.tenant_id = $1 AND p.is_available = true AND p.is_available_online = true
                ORDER BY c.name
            """
            categories_rows = await conn.fetch(categories_query, tenant_id)
            categories = [dict(row) for row in categories_rows]

            products_query = """
                SELECT
                    p.id, p.name, p.description, p.price, p.image_url,
                    p.category_id, c.name as category_name,
                    p.is_available, p.preparation_time,
                    p.allow_modifiers,
                    EXISTS(
                        SELECT 1
                        FROM product_modifier_groups pmg
                        WHERE pmg.product_id = p.id
                    ) as has_modifiers
                FROM product p
                JOIN categories c ON p.category_id = c.id
                WHERE p.tenant_id = $1 AND p.is_available = true AND p.is_available_online = true
            """

            params = [tenant_id]

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
        logger.error(f"Error getting menu by tenant_id '{tenant_id}': {e}")
        raise HTTPException(status_code=500, detail="Error fetching menu")


async def get_product_detail_by_tenant_id(tenant_id: UUID, product_id: UUID) -> Dict[str, Any]:
    """
    Get detailed product information with modifiers, verified by tenant_id.
    Used by API key authenticated endpoints — no slug needed.
    """
    try:
        async with get_db_connection() as conn:
            verification_query = """
                SELECT p.id
                FROM product p
                WHERE p.tenant_id = $1 AND p.id = $2 AND p.is_available = true
            """
            verification = await conn.fetchrow(verification_query, tenant_id, product_id)

            if not verification:
                raise HTTPException(status_code=404, detail="Product not found for this restaurant")

            product_query = """
                SELECT
                    p.id, p.name, p.description, p.price, p.image_url,
                    c.name as category_name,
                    p.is_available, p.is_available_online, p.preparation_time
                FROM product p
                JOIN categories c ON p.category_id = c.id
                WHERE p.id = $1
            """
            product_row = await conn.fetchrow(product_query, product_id)
            product = dict(product_row)
            product['price'] = float(product['price'])

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

            modifier_groups: Dict[str, Any] = {}
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
        logger.error(f"Error getting product detail by tenant_id for product {product_id}: {e}")
        raise HTTPException(status_code=500, detail="Error fetching product details")


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
            # 1. Verify tenant owns this product and it exists in the POS
            # Note: is_available_online is NOT checked here — the detail endpoint
            # returns the product regardless, with is_available_online in the payload
            # so the frontend can show an appropriate message. Only is_available = false
            # (product removed from POS) warrants a 404.
            verification_query = """
                SELECT p.id
                FROM product p
                JOIN tenant_public_profiles tpp ON tpp.tenant_id = p.tenant_id
                WHERE tpp.slug = $1 AND tpp.is_active = true AND p.id = $2
                  AND p.is_available = true
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
                    p.id, p.name, p.description, p.price, p.image_url,
                    c.name as category_name,
                    p.is_available, p.is_available_online, p.preparation_time
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


def is_currently_open(business_hours: Optional[Dict[str, Any]], is_manually_open: bool = True) -> bool:
    """
    Calculate if restaurant is currently open based on business hours and manual toggle.

    Args:
        business_hours: Dict with format {
            "monday": {"open": "09:00", "close": "22:00", "closed": false},
            ...
        }
        is_manually_open: Operator manual override. False = closed regardless of schedule.

    Returns:
        True if currently open, False otherwise
    """
    # Manual override takes priority over schedule
    if not is_manually_open:
        return False

    if not business_hours:
        return False

    try:
        now = datetime.now(tz=BOGOTA_TZ)
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


async def list_restaurants(
    city: Optional[str] = None,
    city_slug: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    List all active public restaurant profiles.

    Args:
        city: Deprecated — exact-match display-name filter (e.g. 'Bogotá').
              Kept for one release while callers migrate to `city_slug`.
        city_slug: Preferred filter (warocol.com#615). Matches the
              normalized slug stored on tenant_public_profiles.city_slug,
              which mirrors public_cities.city_slug.

    Returns:
        List of restaurant profiles with basic information.
    """
    try:
        async with get_db_connection() as conn:
            # Gate by billing: only tenants with an active or past_due
            # subscription appear in the public directory. INNER JOIN drops
            # tenants with no subscription row (free / never paid).
            query = """
                SELECT
                    tpp.id, tpp.tenant_id, tpp.slug, tpp.is_active,
                    tpp.display_name, tpp.description, tpp.logo_url, tpp.banner_url,
                    tpp.phone_number, tpp.email, tpp.address,
                    tpp.city, tpp.city_slug, tpp.country, tpp.neighborhood,
                    tpp.is_manually_open, tpp.business_hours,
                    tpp.created_at, tpp.updated_at
                FROM tenant_public_profiles tpp
                JOIN tenant_subscriptions ts ON ts.tenant_id = tpp.tenant_id
                WHERE tpp.is_active = true
                  AND ts.status IN ('active', 'past_due')
            """
            params: List[Any] = []

            # Prefer city_slug. Fall back to legacy city (display name) for
            # any caller still on the deprecated alias.
            if city_slug:
                params.append(city_slug)
                query += f" AND tpp.city_slug = ${len(params)}"
            elif city:
                params.append(city)
                query += f" AND tpp.city = ${len(params)}"
                logger.warning(
                    "list_restaurants: deprecated 'city' param used "
                    "(value=%r). Migrate caller to 'city_slug'.", city,
                )

            query += " ORDER BY display_name ASC"

            rows = await conn.fetch(query, *params)

            restaurants = []
            for row in rows:
                # Parse business_hours JSONB if stored as string
                business_hours = row["business_hours"]
                if isinstance(business_hours, str):
                    try:
                        business_hours = json.loads(business_hours)
                    except (json.JSONDecodeError, TypeError):
                        business_hours = {}

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
                    "country": row["country"],
                    "city": row["city"],
                    "city_slug": row["city_slug"],
                    "neighborhood": row["neighborhood"],
                    "is_currently_open": is_currently_open(business_hours, row["is_manually_open"]),
                    "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                    "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None
                })

            return restaurants

    except Exception as e:
        logger.error(
            "Error listing restaurants (city=%r, city_slug=%r): %s",
            city, city_slug, e,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Error listing restaurants: {str(e)}"
        )


async def list_cities(include_empty: bool = False) -> List[Dict[str, Any]]:
    """
    Return the curated city catalog (warocol.com#615).

    Used by the `/negocio` city selector (include_empty=True so operators
    see every city even before someone in that city signs up) and by the
    root `/` discovery section (include_empty=False so only populated
    cities surface to customers).

    Args:
        include_empty: When False (default), filter out cities with zero
            active tenants. When True, return every active catalog entry.

    Returns:
        List of dicts with country, city, city_slug, tenant_count.
        Sorted by sort_order then city name.
    """
    try:
        async with get_db_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    pc.country,
                    pc.city,
                    pc.city_slug,
                    pc.sort_order,
                    COUNT(tpp.id) FILTER (
                        WHERE tpp.is_active = true
                          AND ts.status IN ('active', 'past_due')
                    ) AS tenant_count
                FROM public_cities pc
                LEFT JOIN tenant_public_profiles tpp
                       ON tpp.city_slug = pc.city_slug
                LEFT JOIN tenant_subscriptions ts
                       ON ts.tenant_id = tpp.tenant_id
                WHERE pc.is_active = true
                GROUP BY pc.country, pc.city, pc.city_slug, pc.sort_order
                ORDER BY pc.sort_order ASC, pc.city ASC
                """,
            )
            cities = [
                {
                    "country": r["country"],
                    "city": r["city"],
                    "city_slug": r["city_slug"],
                    "tenant_count": int(r["tenant_count"] or 0),
                }
                for r in rows
                if include_empty or (r["tenant_count"] or 0) > 0
            ]
            return cities
    except Exception as e:
        logger.error("Error listing public cities: %s", e)
        raise HTTPException(
            status_code=500,
            detail=f"Error listing cities: {str(e)}"
        )


async def is_city_slug_known(city_slug: str) -> bool:
    """
    Check whether `city_slug` is a recognised active entry in the
    public_cities catalog (warocol.com#615).

    Used by the profile update endpoint to validate operator input and by
    the tenant slug generator to avoid collisions.
    """
    try:
        async with get_db_connection() as conn:
            hit = await conn.fetchval(
                "SELECT 1 FROM public_cities WHERE city_slug = $1 AND is_active = true",
                city_slug,
            )
            return hit is not None
    except Exception as e:
        logger.error("Error checking city slug %r: %s", city_slug, e)
        return False

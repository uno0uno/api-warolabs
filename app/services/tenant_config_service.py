"""
Tenant configuration service - handles tenant public profile management
Requires authentication - these are admin endpoints
"""
from typing import Optional, Dict, Any
from uuid import UUID
from fastapi import Request, HTTPException
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError
from app.models.tenant_public_profile import (
    TenantPublicProfile,
    TenantPublicProfileCreate,
    TenantPublicProfileUpdate,
    TenantPublicProfileResponse,
    ToggleProfileResponse
)
from app.services import public_restaurant_service
import logging

logger = logging.getLogger(__name__)


async def upsert_public_profile(
    request: Request,
    profile_data: TenantPublicProfileCreate
) -> TenantPublicProfileResponse:
    """
    Create or update tenant public profile (admin endpoint)

    Args:
        request: FastAPI request (for session validation)
        profile_data: Profile data to create/update

    Returns:
        TenantPublicProfileResponse with created/updated profile
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id or profile_data.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        # Validate slug is available (excluding current tenant)
        slug_available = await public_restaurant_service.validate_slug_available(
            profile_data.slug,
            exclude_tenant_id=tenant_id
        )

        if not slug_available:
            raise HTTPException(
                status_code=400,
                detail=f"Slug '{profile_data.slug}' is already taken by another restaurant"
            )

        async with get_db_connection() as conn:
            async with conn.transaction():
                # Upsert (insert or update on conflict)
                query = """
                    INSERT INTO tenant_public_profiles (
                        tenant_id, slug, is_active,
                        display_name, description, logo_url, banner_url,
                        phone_number, email, address,
                        city, neighborhood, latitude, longitude,
                        business_hours, social_media,
                        seo_title, seo_description,
                        accepts_online_orders, min_order_amount, estimated_preparation_time,
                        updated_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, CURRENT_TIMESTAMP)
                    ON CONFLICT (tenant_id)
                    DO UPDATE SET
                        slug = EXCLUDED.slug,
                        is_active = EXCLUDED.is_active,
                        display_name = EXCLUDED.display_name,
                        description = EXCLUDED.description,
                        logo_url = EXCLUDED.logo_url,
                        banner_url = EXCLUDED.banner_url,
                        phone_number = EXCLUDED.phone_number,
                        email = EXCLUDED.email,
                        address = EXCLUDED.address,
                        city = EXCLUDED.city,
                        neighborhood = EXCLUDED.neighborhood,
                        latitude = EXCLUDED.latitude,
                        longitude = EXCLUDED.longitude,
                        business_hours = EXCLUDED.business_hours,
                        social_media = EXCLUDED.social_media,
                        seo_title = EXCLUDED.seo_title,
                        seo_description = EXCLUDED.seo_description,
                        accepts_online_orders = EXCLUDED.accepts_online_orders,
                        min_order_amount = EXCLUDED.min_order_amount,
                        estimated_preparation_time = EXCLUDED.estimated_preparation_time,
                        updated_at = CURRENT_TIMESTAMP
                    RETURNING *
                """

                result = await conn.fetchrow(
                    query,
                    tenant_id,
                    profile_data.slug,
                    profile_data.is_active,
                    profile_data.display_name,
                    profile_data.description,
                    profile_data.logo_url,
                    profile_data.banner_url,
                    profile_data.phone_number,
                    profile_data.email,
                    profile_data.address,
                    profile_data.city,
                    profile_data.neighborhood,
                    profile_data.latitude,
                    profile_data.longitude,
                    profile_data.business_hours,
                    profile_data.social_media,
                    profile_data.seo_title,
                    profile_data.seo_description,
                    profile_data.accepts_online_orders,
                    profile_data.min_order_amount,
                    profile_data.estimated_preparation_time
                )

                profile = TenantPublicProfile(**dict(result))

                logger.info(f"Upserted public profile for tenant {tenant_id}")

                return TenantPublicProfileResponse(
                    success=True,
                    data=profile
                )

    except HTTPException:
        raise
    except AuthenticationError:
        raise
    except Exception as e:
        logger.error(f"Error upserting public profile: {e}")
        raise HTTPException(status_code=500, detail="Error saving public profile")


async def update_public_profile(
    request: Request,
    profile_data: TenantPublicProfileUpdate
) -> TenantPublicProfileResponse:
    """
    Update existing tenant public profile (partial update)

    Args:
        request: FastAPI request (for session validation)
        profile_data: Profile data to update (only provided fields)

    Returns:
        TenantPublicProfileResponse with updated profile
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # Check if profile exists
            exists_query = "SELECT id FROM tenant_public_profiles WHERE tenant_id = $1"
            exists = await conn.fetchrow(exists_query, tenant_id)

            if not exists:
                raise HTTPException(
                    status_code=404,
                    detail="Public profile not found. Please create one first."
                )

            # Build dynamic update query
            update_fields = []
            params = []
            param_counter = 1

            # Only update fields that were provided
            data_dict = profile_data.model_dump(exclude_unset=True)

            # Validate slug if it's being changed
            if 'slug' in data_dict:
                slug_available = await public_restaurant_service.validate_slug_available(
                    data_dict['slug'],
                    exclude_tenant_id=tenant_id
                )
                if not slug_available:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Slug '{data_dict['slug']}' is already taken"
                    )

            for field, value in data_dict.items():
                update_fields.append(f"{field} = ${param_counter}")
                params.append(value)
                param_counter += 1

            if not update_fields:
                raise HTTPException(
                    status_code=400,
                    detail="No fields to update"
                )

            # Always update updated_at
            update_fields.append(f"updated_at = CURRENT_TIMESTAMP")

            # Add tenant_id as last parameter for WHERE clause
            params.append(tenant_id)

            query = f"""
                UPDATE tenant_public_profiles
                SET {', '.join(update_fields)}
                WHERE tenant_id = ${param_counter}
                RETURNING *
            """

            result = await conn.fetchrow(query, *params)

            profile = TenantPublicProfile(**dict(result))

            logger.info(f"Updated public profile for tenant {tenant_id}")

            return TenantPublicProfileResponse(
                success=True,
                data=profile
            )

    except HTTPException:
        raise
    except AuthenticationError:
        raise
    except Exception as e:
        logger.error(f"Error updating public profile: {e}")
        raise HTTPException(status_code=500, detail="Error updating public profile")


async def toggle_public_profile(
    request: Request,
    is_active: bool
) -> ToggleProfileResponse:
    """
    Activate or deactivate tenant public profile

    Args:
        request: FastAPI request (for session validation)
        is_active: True to activate, False to deactivate

    Returns:
        ToggleProfileResponse with success message
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            query = """
                UPDATE tenant_public_profiles
                SET is_active = $1, updated_at = CURRENT_TIMESTAMP
                WHERE tenant_id = $2
                RETURNING id
            """

            result = await conn.fetchrow(query, is_active, tenant_id)

            if not result:
                raise HTTPException(
                    status_code=404,
                    detail="Public profile not found. Please create one first."
                )

            action = "activated" if is_active else "deactivated"
            logger.info(f"Public profile {action} for tenant {tenant_id}")

            return ToggleProfileResponse(
                success=True,
                message=f"Public profile {action} successfully",
                is_active=is_active
            )

    except HTTPException:
        raise
    except AuthenticationError:
        raise
    except Exception as e:
        logger.error(f"Error toggling public profile: {e}")
        raise HTTPException(status_code=500, detail="Error toggling public profile")


async def get_own_public_profile(request: Request) -> Optional[TenantPublicProfileResponse]:
    """
    Get own tenant public profile (for admin view)

    Args:
        request: FastAPI request (for session validation)

    Returns:
        TenantPublicProfileResponse or None if not found
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            query = """
                SELECT * FROM tenant_public_profiles
                WHERE tenant_id = $1
            """

            result = await conn.fetchrow(query, tenant_id)

            if not result:
                return None

            profile = TenantPublicProfile(**dict(result))

            return TenantPublicProfileResponse(
                success=True,
                data=profile
            )

    except AuthenticationError:
        raise
    except Exception as e:
        logger.error(f"Error getting own public profile: {e}")
        raise HTTPException(status_code=500, detail="Error fetching public profile")

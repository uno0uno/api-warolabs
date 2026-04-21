"""
Tenant configuration service - handles tenant public profile management
Requires authentication - these are admin endpoints
"""
import asyncio
import json
from typing import Optional, Literal
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
from app.services.aws_s3_service import AWSS3Service
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
                        tables_enabled,
                        comandas_enabled, kds_enabled,
                        updated_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23, $24, CURRENT_TIMESTAMP)
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
                        tables_enabled = EXCLUDED.tables_enabled,
                        comandas_enabled = EXCLUDED.comandas_enabled,
                        kds_enabled = EXCLUDED.kds_enabled,
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
                    json.dumps(profile_data.business_hours) if profile_data.business_hours is not None else None,
                    json.dumps(profile_data.social_media) if profile_data.social_media is not None else None,
                    profile_data.seo_title,
                    profile_data.seo_description,
                    profile_data.accepts_online_orders,
                    profile_data.min_order_amount,
                    profile_data.estimated_preparation_time,
                    profile_data.tables_enabled,
                    profile_data.comandas_enabled,
                    profile_data.kds_enabled
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
                # First-time creation: auto-generate slug from tenant's own slug
                tenant_row = await conn.fetchrow(
                    "SELECT slug, name FROM tenants WHERE id = $1", tenant_id
                )
                if not tenant_row:
                    raise HTTPException(status_code=404, detail="Tenant not found")

                data_dict = profile_data.model_dump(exclude_unset=True)
                display_name = data_dict.get('display_name') or tenant_row['name']

                insert_query = """
                    INSERT INTO tenant_public_profiles (
                        tenant_id, slug, display_name, is_active,
                        description, logo_url, banner_url,
                        phone_number, email, address, city, neighborhood,
                        business_hours, social_media,
                        accepts_online_orders, min_order_amount, estimated_preparation_time,
                        is_manually_open
                    ) VALUES ($1, $2, $3, FALSE, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17)
                    RETURNING *
                """
                result = await conn.fetchrow(
                    insert_query,
                    tenant_id,
                    tenant_row['slug'],
                    display_name,
                    data_dict.get('description'),
                    data_dict.get('logo_url'),
                    data_dict.get('banner_url'),
                    data_dict.get('phone_number'),
                    data_dict.get('email'),
                    data_dict.get('address'),
                    data_dict.get('city'),
                    data_dict.get('neighborhood'),
                    json.dumps(data_dict['business_hours']) if data_dict.get('business_hours') is not None else None,
                    json.dumps(data_dict['social_media']) if data_dict.get('social_media') is not None else None,
                    data_dict.get('accepts_online_orders', False),
                    data_dict.get('min_order_amount', 0),
                    data_dict.get('estimated_preparation_time', 30),
                    data_dict.get('is_manually_open', True),
                )

                profile_data_dict = dict(result)
                if isinstance(profile_data_dict.get('business_hours'), str):
                    try:
                        profile_data_dict['business_hours'] = json.loads(profile_data_dict['business_hours'])
                    except Exception:
                        profile_data_dict['business_hours'] = None
                if isinstance(profile_data_dict.get('social_media'), str):
                    try:
                        profile_data_dict['social_media'] = json.loads(profile_data_dict['social_media'])
                    except Exception:
                        profile_data_dict['social_media'] = None

                profile = TenantPublicProfile(**profile_data_dict)
                logger.info(f"Created new public profile for tenant {tenant_id} via PATCH upsert")
                return TenantPublicProfileResponse(success=True, data=profile)

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

            _jsonb_fields = {'business_hours', 'social_media'}
            for field, value in data_dict.items():
                update_fields.append(f"{field} = ${param_counter}")
                params.append(json.dumps(value) if field in _jsonb_fields and value is not None else value)
                param_counter += 1

            if not update_fields:
                raise HTTPException(
                    status_code=400,
                    detail="No fields to update"
                )

            # Always update updated_at
            update_fields.append("updated_at = CURRENT_TIMESTAMP")

            # Add tenant_id as last parameter for WHERE clause
            params.append(tenant_id)

            query = f"""
                UPDATE tenant_public_profiles
                SET {', '.join(update_fields)}
                WHERE tenant_id = ${param_counter}
                RETURNING *
            """

            result = await conn.fetchrow(query, *params)

            profile_data_dict = dict(result)
            if isinstance(profile_data_dict.get('business_hours'), str):
                try:
                    profile_data_dict['business_hours'] = json.loads(profile_data_dict['business_hours'])
                except Exception:
                    profile_data_dict['business_hours'] = None
            if isinstance(profile_data_dict.get('social_media'), str):
                try:
                    profile_data_dict['social_media'] = json.loads(profile_data_dict['social_media'])
                except Exception:
                    profile_data_dict['social_media'] = None

            profile = TenantPublicProfile(**profile_data_dict)

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
            # Fetch current state to detect first-ever activation
            current_row = await conn.fetchrow(
                "SELECT is_active, email, display_name, welcome_email_sent FROM tenant_public_profiles WHERE tenant_id = $1",
                tenant_id
            )

            if not current_row:
                raise HTTPException(
                    status_code=404,
                    detail="Public profile not found. Please create one first."
                )

            was_inactive = not current_row['is_active']
            already_welcomed = current_row['welcome_email_sent']

            await conn.execute(
                "UPDATE tenant_public_profiles SET is_active = $1, updated_at = CURRENT_TIMESTAMP WHERE tenant_id = $2",
                is_active, tenant_id
            )

            # Send welcome email only on the very first activation ever
            if is_active and was_inactive and not already_welcomed:
                profile_email = current_row['email']
                profile_name = current_row['display_name']
                if profile_email:
                    from app.services import email_helpers
                    asyncio.create_task(
                        email_helpers.send_negocio_welcome_email(profile_email, profile_name)
                    )
                    await conn.execute(
                        "UPDATE tenant_public_profiles SET welcome_email_sent = true WHERE tenant_id = $1",
                        tenant_id
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

            profile_data = dict(result)

            # asyncpg returns JSONB columns as strings — parse them
            if isinstance(profile_data.get('business_hours'), str):
                try:
                    profile_data['business_hours'] = json.loads(profile_data['business_hours'])
                except Exception:
                    profile_data['business_hours'] = None

            if isinstance(profile_data.get('social_media'), str):
                try:
                    profile_data['social_media'] = json.loads(profile_data['social_media'])
                except Exception:
                    profile_data['social_media'] = None

            profile_data['is_currently_open'] = public_restaurant_service.is_currently_open(
                profile_data.get('business_hours'),
                profile_data.get('is_manually_open', True),
            )

            profile = TenantPublicProfile(**profile_data)

            return TenantPublicProfileResponse(
                success=True,
                data=profile
            )

    except AuthenticationError:
        raise
    except Exception as e:
        logger.error(f"Error getting own public profile: {e}")
        raise HTTPException(status_code=500, detail="Error fetching public profile")


async def upload_tenant_image(
    request: Request,
    file_bytes: bytes,
    filename: str,
    content_type: str,
    image_type: Literal['logo', 'banner'],
) -> str:
    """
    Upload a tenant profile image (logo or banner) to the public R2 bucket.

    Args:
        request: FastAPI request (for session validation)
        file_bytes: Raw image bytes (already compressed client-side)
        filename: Original filename
        content_type: MIME type of the image
        image_type: 'logo' or 'banner'

    Returns:
        Permanent public URL of the uploaded image

    Raises:
        HTTPException 503 if public R2 bucket is not configured
        HTTPException 500 if upload fails
    """
    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id

    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    s3_service = AWSS3Service()

    public_url = await s3_service.upload_public_image(
        file_bytes=file_bytes,
        filename=filename,
        tenant_id=str(tenant_id),
        image_type=image_type,
        content_type=content_type,
    )

    if public_url is None:
        from app.config import settings
        if not settings.r2_public_url:
            raise HTTPException(
                status_code=503,
                detail="Public image storage not configured. Set NUXT_PRIVATE_R2_PUBLIC_URL and create the warocol-public-assets bucket."
            )
        raise HTTPException(status_code=500, detail="Failed to upload image")

    return public_url


async def get_tax_config(request: Request) -> dict:
    """
    Return the tax configuration for the active tenant.
    If no row exists yet, inserts defaults and returns them.
    """
    try:
        session = require_valid_session(request)
        tenant_id = session.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM tenant_tax_config WHERE tenant_id = $1",
                tenant_id,
            )
            if not row:
                await conn.execute(
                    "INSERT INTO tenant_tax_config (tenant_id) VALUES ($1) ON CONFLICT DO NOTHING",
                    tenant_id,
                )
                row = await conn.fetchrow(
                    "SELECT * FROM tenant_tax_config WHERE tenant_id = $1",
                    tenant_id,
                )

            return {"success": True, "data": dict(row)}

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error fetching tax config: {e}")
        raise HTTPException(status_code=500, detail=f"Error fetching tax config: {str(e)}")


async def update_tax_config(request: Request, data) -> dict:
    """
    Upsert the tax configuration for the active tenant.
    Accepts a TaxConfigUpdate-shaped object.
    """
    try:
        session = require_valid_session(request)
        tenant_id = session.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO tenant_tax_config (
                    tenant_id,
                    inc_applicable, inc_included_in_price,
                    iva_applicable, iva_included_in_price,
                    liquor_tax_applicable
                )
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (tenant_id) DO UPDATE SET
                    inc_applicable        = EXCLUDED.inc_applicable,
                    inc_included_in_price = EXCLUDED.inc_included_in_price,
                    iva_applicable        = EXCLUDED.iva_applicable,
                    iva_included_in_price = EXCLUDED.iva_included_in_price,
                    liquor_tax_applicable = EXCLUDED.liquor_tax_applicable,
                    updated_at            = NOW()
                RETURNING *
                """,
                tenant_id,
                data.inc_applicable,
                data.inc_included_in_price,
                data.iva_applicable,
                data.iva_included_in_price,
                data.liquor_tax_applicable,
            )

            return {"success": True, "data": dict(row)}

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error updating tax config: {e}")
        raise HTTPException(status_code=500, detail=f"Error updating tax config: {str(e)}")

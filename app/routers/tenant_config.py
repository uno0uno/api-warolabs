"""
Tenant configuration router - admin endpoints for managing public profiles
Authentication required
"""
from fastapi import APIRouter, Request, Body, HTTPException
from fastapi.responses import JSONResponse
from app.services import tenant_config_service
from app.models.tenant_public_profile import (
    TenantPublicProfileCreate,
    TenantPublicProfileUpdate,
    TenantPublicProfileResponse,
    ToggleProfileRequest,
    ToggleProfileResponse
)
from app.core.exceptions import AuthenticationError
from typing import Optional

router = APIRouter()


@router.get("/public-profile", response_model=Optional[TenantPublicProfileResponse])
async def get_own_public_profile_endpoint(
    request: Request
):
    """
    Get own tenant public profile (admin view)

    Returns the public profile configuration for the current tenant.
    Used in admin panel to view/edit profile settings.

    **Requires authentication**
    """
    return await tenant_config_service.get_own_public_profile(request)


@router.put("/public-profile", response_model=TenantPublicProfileResponse)
async def upsert_public_profile_endpoint(
    request: Request,
    profile_data: TenantPublicProfileCreate = Body(...)
):
    """
    Create or update tenant public profile (full replace)

    If profile exists, it will be completely replaced.
    If not, a new profile will be created.

    Required fields:
    - tenant_id: UUID
    - slug: URL-friendly slug (must be unique)
    - display_name: Public restaurant name
    - is_active: Whether profile is public (true/false)

    Optional fields:
    - description, logo_url, banner_url
    - phone_number, email, address
    - city, neighborhood, latitude, longitude
    - business_hours: JSON object with schedule
    - social_media: JSON object with social links
    - seo_title, seo_description
    - accepts_online_orders (future)
    - min_order_amount, estimated_preparation_time

    **Requires authentication**
    """
    return await tenant_config_service.upsert_public_profile(request, profile_data)


@router.patch("/public-profile", response_model=TenantPublicProfileResponse)
async def update_public_profile_endpoint(
    request: Request,
    profile_data: TenantPublicProfileUpdate = Body(...)
):
    """
    Update tenant public profile (partial update)

    Only updates the fields provided in the request.
    All fields are optional.

    Use this endpoint to update specific fields without replacing the entire profile.

    **Requires authentication**
    """
    return await tenant_config_service.update_public_profile(request, profile_data)


@router.post("/public-profile/toggle", response_model=ToggleProfileResponse)
async def toggle_public_profile_endpoint(
    request: Request,
    toggle_data: ToggleProfileRequest = Body(...)
):
    """
    Activate or deactivate tenant public profile

    Request body:
    {
        "is_active": true  // true to activate, false to deactivate
    }

    When deactivated, the public profile will not be visible to customers.

    **Requires authentication**
    """
    return await tenant_config_service.toggle_public_profile(
        request,
        toggle_data.is_active
    )


ALLOWED_IMAGE_TYPES = {'image/jpeg', 'image/png', 'image/webp'}
MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5MB


@router.post("/upload-image")
async def upload_tenant_image_endpoint(request: Request):
    """
    Upload a logo or banner image for the tenant public profile.

    Accepts multipart/form-data with:
    - file: image file (JPEG, PNG, WebP only, max 5MB)
    - image_type: 'logo' or 'banner'

    Returns:
    {
        "url": "https://pub-....r2.dev/tenant-profiles/..."
    }

    The returned URL is permanent (no expiry) via Cloudflare R2 Public Development URL.

    **Requires authentication**
    """
    try:
        form = await request.form()
        file = form.get("file")
        image_type = form.get("image_type", "logo")

        if not file or not hasattr(file, 'read'):
            raise HTTPException(status_code=400, detail="No file provided")

        if image_type not in ('logo', 'banner'):
            raise HTTPException(status_code=400, detail="image_type must be 'logo' or 'banner'")

        content_type = getattr(file, 'content_type', None) or 'application/octet-stream'
        if content_type not in ALLOWED_IMAGE_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"File type not allowed. Use JPEG, PNG, or WebP. Got: {content_type}"
            )

        file_bytes = await file.read()

        if len(file_bytes) > MAX_IMAGE_SIZE:
            raise HTTPException(status_code=400, detail="File too large. Maximum size is 5MB.")

        public_url = await tenant_config_service.upload_tenant_image(
            request=request,
            file_bytes=file_bytes,
            filename=getattr(file, 'filename', 'image.jpg') or 'image.jpg',
            content_type=content_type,
            image_type=image_type,
        )

        return JSONResponse({"url": public_url})

    except HTTPException:
        raise
    except AuthenticationError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")

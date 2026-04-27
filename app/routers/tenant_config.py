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
from app.models.tax_config import TaxConfigUpdate
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


@router.get("/tax-config")
async def get_tax_config_endpoint(request: Request):
    """
    Get the fiscal tax configuration for the active tenant.
    If no row exists, inserts defaults and returns them.

    **Requires authentication**
    """
    return await tenant_config_service.get_tax_config(request)


@router.put("/tax-config")
async def update_tax_config_endpoint(
    request: Request,
    data: TaxConfigUpdate = Body(...),
):
    """
    Update the fiscal tax configuration for the active tenant.

    Request body:
    {
        "inc_applicable": true,
        "inc_included_in_price": true,
        "iva_applicable": false,
        "iva_included_in_price": false,
        "liquor_tax_applicable": false
    }

    **Requires authentication**
    """
    return await tenant_config_service.update_tax_config(request, data)


@router.get("/dian-resolutions")
async def get_dian_resolutions(request: Request):
    """
    Get all DIAN resolutions for the active tenant.
    Returns resolution details with usage stats.
    """
    from app.core.middleware import require_valid_session
    from app.database import get_db_connection

    session = require_valid_session(request)
    tenant_id = session.tenant_id

    async with get_db_connection() as conn:
        rows = await conn.fetch(
            """SELECT id, resolution_number, prefix, date_from, date_to,
                      from_number, to_number, current_number, is_active, document_type, created_at
               FROM dian_resolutions
               WHERE tenant_id = $1
               ORDER BY is_active DESC, created_at DESC""",
            tenant_id,
        )

    resolutions = []
    for r in rows:
        total_range = r['to_number'] - r['from_number'] + 1
        used = r['current_number'] - r['from_number'] + 1 if r['current_number'] >= r['from_number'] else 0
        usage_percent = round((used / total_range) * 100, 1) if total_range > 0 else 0

        resolutions.append({
            'id': str(r['id']),
            'resolution_number': r['resolution_number'],
            'prefix': r['prefix'],
            'date_from': r['date_from'].isoformat(),
            'date_to': r['date_to'].isoformat(),
            'from_number': r['from_number'],
            'to_number': r['to_number'],
            'current_number': r['current_number'],
            'is_active': r['is_active'],
            'document_type': r['document_type'],
            'total_range': total_range,
            'used': used,
            'usage_percent': usage_percent,
        })

    return {'success': True, 'data': resolutions}


@router.get("/facturacion-status")
async def get_facturacion_status(request: Request):
    """
    Get Matias API connection status and last emitted document.
    """
    from app.core.middleware import require_valid_session
    from app.database import get_db_connection

    session = require_valid_session(request)
    tenant_id = session.tenant_id

    async with get_db_connection() as conn:
        last_doc = await conn.fetchrow(
            """SELECT prefix, invoice_number, status, document_type, created_at
               FROM electronic_invoices
               WHERE tenant_id = $1
               ORDER BY created_at DESC
               LIMIT 1""",
            tenant_id,
        )

    last_document = None
    if last_doc:
        last_document = {
            'prefix': last_doc['prefix'],
            'invoice_number': last_doc['invoice_number'],
            'status': last_doc['status'],
            'document_type': last_doc['document_type'],
            'created_at': last_doc['created_at'].isoformat() if last_doc['created_at'] else None,
        }

    return {
        'success': True,
        'data': {
            'environment': 'Habilitación (pruebas)',
            'last_document': last_document,
        },
    }


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

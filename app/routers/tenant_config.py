"""
Tenant configuration router - admin endpoints for managing public profiles
Authentication required
"""
from datetime import date as _date
from asyncpg.exceptions import UniqueViolationError
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


@router.get("/invoicing-readiness")
async def get_invoicing_readiness_endpoint(request: Request):
    """
    Return whether the active tenant can emit electronic invoices right now.

    Three predicates must all be true: dev_flag_enabled, fiscal_data_complete,
    active_resolution. See `app/services/invoicing_readiness_service.py` for
    the full contract.

    **Requires authentication**
    """
    from app.core.middleware import require_valid_session
    from app.services import invoicing_readiness_service

    session = require_valid_session(request)
    payload = await invoicing_readiness_service.get_readiness(session.tenant_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return payload


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


@router.get("/fiscal-data")
async def get_fiscal_data(request: Request):
    """
    Get fiscal data for the active tenant.
    Inserts defaults on first access if no row exists.
    """
    from app.core.middleware import require_valid_session
    from app.database import get_db_connection

    session = require_valid_session(request)
    tenant_id = session.tenant_id

    async with get_db_connection() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM tenant_fiscal_data WHERE tenant_id = $1", tenant_id,
        )
        if not row:
            await conn.execute(
                "INSERT INTO tenant_fiscal_data (tenant_id) VALUES ($1) ON CONFLICT DO NOTHING",
                tenant_id,
            )
            row = await conn.fetchrow(
                "SELECT * FROM tenant_fiscal_data WHERE tenant_id = $1", tenant_id,
            )

    return {
        'success': True,
        'data': {
            'nit': row['nit'],
            'business_name': row['business_name'],
            'type_organization_id': row['type_organization_id'],
            'tax_regime_id': row['tax_regime_id'],
            'tax_level_id': row['tax_level_id'],
            'fiscal_address': row['fiscal_address'],
            'city': row['city'],
            'city_id': row['city_id'],
            'phone': row['phone'],
            'email': row['email'],
        },
    }


@router.put("/fiscal-data")
async def update_fiscal_data(request: Request, data: dict = Body(...)):
    """
    Upsert fiscal data for the active tenant.
    """
    from app.core.middleware import require_valid_session
    from app.database import get_db_connection

    session = require_valid_session(request)
    tenant_id = session.tenant_id

    async with get_db_connection() as conn:
        await conn.execute(
            """INSERT INTO tenant_fiscal_data (tenant_id, nit, business_name,
                   type_organization_id, tax_regime_id, tax_level_id,
                   fiscal_address, city, city_id, phone, email, updated_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, now())
               ON CONFLICT (tenant_id) DO UPDATE SET
                   nit = EXCLUDED.nit,
                   business_name = EXCLUDED.business_name,
                   type_organization_id = EXCLUDED.type_organization_id,
                   tax_regime_id = EXCLUDED.tax_regime_id,
                   tax_level_id = EXCLUDED.tax_level_id,
                   fiscal_address = EXCLUDED.fiscal_address,
                   city = EXCLUDED.city,
                   city_id = EXCLUDED.city_id,
                   phone = EXCLUDED.phone,
                   email = EXCLUDED.email,
                   updated_at = now()""",
            tenant_id,
            data.get('nit'),
            data.get('business_name'),
            data.get('type_organization_id', 1),
            data.get('tax_regime_id', 2),
            data.get('tax_level_id', 5),
            data.get('fiscal_address'),
            data.get('city'),
            data.get('city_id', 149),
            data.get('phone'),
            data.get('email'),
        )

    return {'success': True, 'message': 'Datos fiscales actualizados'}


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


@router.post("/dian-resolutions")
async def create_dian_resolution(request: Request, data: dict = Body(...)):
    """Create a new DIAN resolution for the active tenant."""
    from app.core.middleware import require_valid_session
    from app.database import get_db_connection
    import uuid as _uuid

    session = require_valid_session(request)
    tenant_id = session.tenant_id

    prefix = data.get('prefix', '').strip().upper()
    resolution_number = data.get('resolution_number', '').strip()
    from_number = int(data.get('from_number', 0))
    to_number = int(data.get('to_number', 0))
    # asyncpg requires real date objects for `date` columns — the SQL ::date
    # cast happens AFTER parameter binding, so passing a 'YYYY-MM-DD' string
    # raises AttributeError ('str' has no 'toordinal'). Parse here.
    raw_from = data.get('date_from')
    raw_to = data.get('date_to')
    if not raw_from or not raw_to:
        raise HTTPException(status_code=422, detail="date_from y date_to son requeridos")
    try:
        date_from = _date.fromisoformat(raw_from)
        date_to = _date.fromisoformat(raw_to)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Formato de fecha inválido. Usa YYYY-MM-DD")
    document_type = data.get('document_type', 'invoice')
    current_number = int(data.get('current_number', from_number - 1))

    if not prefix or not resolution_number or from_number <= 0 or to_number <= 0:
        raise HTTPException(status_code=422, detail="Prefijo, número de resolución y rango son requeridos")
    if from_number >= to_number:
        raise HTTPException(status_code=422, detail="El rango 'desde' debe ser menor que 'hasta'")

    async with get_db_connection() as conn:
        row_id = _uuid.uuid4()
        try:
            await conn.execute(
                """INSERT INTO dian_resolutions
                       (id, tenant_id, resolution_number, prefix, date_from, date_to,
                        from_number, to_number, current_number, is_active, document_type)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, true, $10)""",
                row_id, tenant_id, resolution_number, prefix,
                date_from, date_to, from_number, to_number, current_number, document_type,
            )
        except UniqueViolationError as e:
            # Partial unique index idx_dian_resolutions_active_prefix:
            # only one ACTIVE resolution per (tenant, prefix) is allowed.
            if 'idx_dian_resolutions_active_prefix' in str(e):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Ya existe una resolución activa con prefijo '{prefix}'. "
                        "Desactívala primero o usa un prefijo diferente."
                    ),
                )
            raise

    return {'success': True, 'data': {'id': str(row_id)}, 'message': 'Resolución creada'}


@router.put("/dian-resolutions/{resolution_id}")
async def update_dian_resolution(request: Request, resolution_id: str, data: dict = Body(...)):
    """Update an existing DIAN resolution."""
    from app.core.middleware import require_valid_session
    from app.database import get_db_connection

    session = require_valid_session(request)
    tenant_id = session.tenant_id

    # Parse dates (asyncpg requires date objects, not strings — same reason
     # as the POST endpoint above).
    raw_from = data.get('date_from')
    raw_to = data.get('date_to')
    if not raw_from or not raw_to:
        raise HTTPException(status_code=422, detail="date_from y date_to son requeridos")
    try:
        date_from = _date.fromisoformat(raw_from)
        date_to = _date.fromisoformat(raw_to)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Formato de fecha inválido. Usa YYYY-MM-DD")

    async with get_db_connection() as conn:
        result = await conn.execute(
            """UPDATE dian_resolutions
               SET resolution_number = $1, prefix = $2, date_from = $3, date_to = $4,
                   from_number = $5, to_number = $6, current_number = $7, document_type = $8
               WHERE id = $9::uuid AND tenant_id = $10""",
            data.get('resolution_number'), data.get('prefix', '').strip().upper(),
            date_from, date_to,
            int(data.get('from_number', 0)), int(data.get('to_number', 0)),
            int(data.get('current_number', 0)), data.get('document_type', 'invoice'),
            resolution_id, tenant_id,
        )

    return {'success': True, 'message': 'Resolución actualizada'}


@router.patch("/dian-resolutions/{resolution_id}/toggle")
async def toggle_dian_resolution(request: Request, resolution_id: str):
    """Toggle is_active for a DIAN resolution."""
    from app.core.middleware import require_valid_session
    from app.database import get_db_connection

    session = require_valid_session(request)
    tenant_id = session.tenant_id

    async with get_db_connection() as conn:
        row = await conn.fetchrow(
            "SELECT is_active FROM dian_resolutions WHERE id = $1::uuid AND tenant_id = $2",
            resolution_id, tenant_id,
        )
        if not row:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Resolución no encontrada")

        new_state = not row['is_active']
        await conn.execute(
            "UPDATE dian_resolutions SET is_active = $1 WHERE id = $2::uuid AND tenant_id = $3",
            new_state, resolution_id, tenant_id,
        )

    return {'success': True, 'data': {'is_active': new_state}}


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

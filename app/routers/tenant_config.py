"""
Tenant configuration router - admin endpoints for managing public profiles
Authentication required
"""
from datetime import date as _date
from uuid import UUID
from asyncpg.exceptions import UniqueViolationError
from fastapi import APIRouter, Depends, Request, Body, HTTPException
from fastapi.responses import JSONResponse
from app.core.permissions import Module, require_module
from app.services.account_role_service import require_matias_dian_capability
from app.services import tenant_config_service
from app.models.tenant_public_profile import (
    TenantPublicProfileCreate,
    TenantPublicProfileUpdate,
    TenantPublicProfileResponse,
    ToggleProfileRequest,
    ToggleProfileResponse
)
from app.models.tenant_financial_profile import (
    TenantFinancialProfileResponse,
    TenantFinancialProfileUpdate,
)
from app.services import tenant_financial_profile_service
from app.models.tax_config import TaxConfigUpdate
from app.core.exceptions import AuthenticationError
from app.core.sales_tax_profile import (
    ALLOWED_SALES_TAX_PROFILES,
    settings_for_sales_tax_profile,
)
from typing import Optional

router = APIRouter()


@router.get(
    "/financial-profile",
    response_model=TenantFinancialProfileResponse,
)
async def get_financial_profile_endpoint(request: Request):
    """Return tenant financial capabilities to any authenticated tenant member."""
    return await tenant_financial_profile_service.get_financial_profile(request)


@router.put(
    "/financial-profile",
    response_model=TenantFinancialProfileResponse,
    dependencies=[Depends(require_module(Module.MI_NEGOCIO))],
)
async def update_financial_profile_endpoint(
    request: Request,
    profile_data: TenantFinancialProfileUpdate = Body(...),
):
    """Atomically change country/base currency when tenant activity allows it."""
    return await tenant_financial_profile_service.update_financial_profile(
        request, profile_data
    )


@router.get("/public-profile", response_model=Optional[TenantPublicProfileResponse], dependencies=[Depends(require_module(Module.MI_NEGOCIO))])
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


@router.put("/public-profile", response_model=TenantPublicProfileResponse, dependencies=[Depends(require_module(Module.MI_NEGOCIO))])
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


@router.patch("/public-profile", response_model=TenantPublicProfileResponse, dependencies=[Depends(require_module(Module.MI_NEGOCIO))])
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


@router.post("/public-profile/toggle", response_model=ToggleProfileResponse, dependencies=[Depends(require_module(Module.MI_NEGOCIO))])
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


@router.get("/invoicing-readiness", dependencies=[Depends(require_module(Module.MI_NEGOCIO))])
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


@router.get("/tax-config", dependencies=[Depends(require_module(Module.MI_NEGOCIO))])
async def get_tax_config_endpoint(request: Request):
    """
    Get the fiscal tax configuration for the active tenant.
    If no row exists, inserts defaults and returns them.

    **Requires authentication**
    """
    return await tenant_config_service.get_tax_config(request)


@router.put("/tax-config", dependencies=[Depends(require_module(Module.MI_NEGOCIO))])
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


def _normalize_receipt_document_label(raw) -> str:
    label = (raw or 'Prefactura').strip()
    if not label:
        return 'Prefactura'
    if len(label) > 40:
        raise HTTPException(status_code=400, detail='Document label must be at most 40 characters')
    return label


def _normalize_receipt_tip_label(raw) -> str:
    label = (raw or 'Propina').strip()
    if not label:
        return 'Propina'
    if len(label) > 40:
        raise HTTPException(status_code=400, detail='Tip label must be at most 40 characters')
    return label


def _normalize_matias_company_id(data: dict) -> Optional[str]:
    raw = data.get('matias_company_id')
    if raw is None and 'client_uuid' in data:
        raw = data.get('client_uuid')
    if raw is None and 'companyId' in data:
        raw = data.get('companyId')
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise HTTPException(status_code=400, detail='Matias companyId must be a valid UUID string')

    value = raw.strip()
    if not value:
        return None

    try:
        UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail='Matias companyId must be a valid UUID') from exc
    return value


def _normalize_sales_tax_profile(data: dict) -> str:
    raw = data.get('sales_tax_profile', 'unconfigured')
    if not isinstance(raw, str):
        raise HTTPException(
            status_code=400,
            detail='El perfil tributario de ventas debe ser un texto válido',
        )
    value = raw.strip().lower()
    if value not in ALLOWED_SALES_TAX_PROFILES:
        raise HTTPException(
            status_code=400,
            detail='Perfil tributario de ventas no soportado por WARO/Matias',
        )
    return value


@router.get("/fiscal-data", dependencies=[Depends(require_module(Module.MI_NEGOCIO))])
async def get_fiscal_data(request: Request):
    """
    Get fiscal data for the active tenant.
    Inserts defaults on first access if no row exists.

    Fiscal data identifies the electronic invoice issuer. Sales tax toggles
    such as INC/IVA live in tenant_tax_config and are handled separately.
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
            'sales_tax_profile': row.get('sales_tax_profile', 'unconfigured'),
            'fiscal_address': row['fiscal_address'],
            'city': row['city'],
            'city_id': row['city_id'],
            'phone': row['phone'],
            'email': row['email'],
            'electronic_invoicing_requested': bool(row['electronic_invoicing_requested']),
            'matias_company_id': row['matias_company_id'],
            'receipt_document_label': _normalize_receipt_document_label(row['receipt_document_label']),
            'receipt_tip_label': _normalize_receipt_tip_label(row.get('receipt_tip_label')),
            'show_logo_on_receipts': row['show_logo_on_receipts']
            if row['show_logo_on_receipts'] is not None
            else True,
        },
    }


@router.put("/fiscal-data", dependencies=[Depends(require_module(Module.MI_NEGOCIO))])
async def update_fiscal_data(request: Request, data: dict = Body(...)):
    """
    Upsert fiscal data for the active tenant.

    This endpoint intentionally does not mutate tenant_tax_config. A tenant can
    update organization type or IVA responsibility without auto-enabling INC/IVA
    on sale lines.
    """
    from app.core.middleware import require_valid_session
    from app.database import get_db_connection

    session = require_valid_session(request)
    tenant_id = session.tenant_id

    document_label = _normalize_receipt_document_label(data.get('receipt_document_label'))
    tip_label = _normalize_receipt_tip_label(data.get('receipt_tip_label'))
    show_logo = bool(data.get('show_logo_on_receipts', True))
    matias_company_id = _normalize_matias_company_id(data)
    electronic_invoicing_requested = bool(data.get('electronic_invoicing_requested', False))
    sales_tax_profile = _normalize_sales_tax_profile(data)

    type_organization_id = data.get('type_organization_id', 1)
    tax_level_id = data.get('tax_level_id', 5)
    if type_organization_id not in (1, 2):
        raise HTTPException(status_code=400, detail='Tipo de organización no soportado por Matias')
    if tax_level_id not in (1, 2, 3, 4, 5):
        raise HTTPException(status_code=400, detail='Responsabilidad tributaria no soportada por Matias')
    if sales_tax_profile == 'non_responsible_iva_inc' and type_organization_id != 2:
        raise HTTPException(
            status_code=400,
            detail=(
                'El perfil no responsable de IVA e INC (RUT 49 + 50) '
                'solo aplica a persona natural'
            ),
        )

    profile_settings = settings_for_sales_tax_profile(sales_tax_profile)
    if profile_settings:
        tax_regime_id = profile_settings['tax_regime_id']
    else:
        tax_regime_id = data.get('tax_regime_id', 2)
        if tax_regime_id not in (1, 2):
            raise HTTPException(status_code=400, detail='Régimen de IVA no soportado por Matias')

    async with get_db_connection() as conn:
        await conn.execute(
            """INSERT INTO tenant_fiscal_data (tenant_id, nit, business_name,
                   type_organization_id, tax_regime_id, tax_level_id, sales_tax_profile,
                   fiscal_address, city, city_id, phone, email,
                   electronic_invoicing_requested, matias_company_id,
                   receipt_document_label, receipt_tip_label, show_logo_on_receipts,
                   updated_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, now())
               ON CONFLICT (tenant_id) DO UPDATE SET
                   nit = EXCLUDED.nit,
                   business_name = EXCLUDED.business_name,
                   type_organization_id = EXCLUDED.type_organization_id,
                   tax_regime_id = EXCLUDED.tax_regime_id,
                   tax_level_id = EXCLUDED.tax_level_id,
                   sales_tax_profile = EXCLUDED.sales_tax_profile,
                   fiscal_address = EXCLUDED.fiscal_address,
                   city = EXCLUDED.city,
                   city_id = EXCLUDED.city_id,
                   phone = EXCLUDED.phone,
                   email = EXCLUDED.email,
                   electronic_invoicing_requested = EXCLUDED.electronic_invoicing_requested,
                   matias_company_id = EXCLUDED.matias_company_id,
                   receipt_document_label = EXCLUDED.receipt_document_label,
                   receipt_tip_label = EXCLUDED.receipt_tip_label,
                   show_logo_on_receipts = EXCLUDED.show_logo_on_receipts,
                   updated_at = now()""",
            tenant_id,
            data.get('nit'),
            data.get('business_name'),
            type_organization_id,
            tax_regime_id,
            tax_level_id,
            sales_tax_profile,
            data.get('fiscal_address'),
            data.get('city'),
            data.get('city_id', 149),
            data.get('phone'),
            data.get('email'),
            electronic_invoicing_requested,
            matias_company_id,
            document_label,
            tip_label,
            show_logo,
        )

        if profile_settings:
            await conn.execute(
                """INSERT INTO tenant_tax_config (
                       tenant_id, inc_applicable, iva_applicable, updated_at
                   )
                   VALUES ($1, $2, $3, now())
                   ON CONFLICT (tenant_id) DO UPDATE SET
                       inc_applicable = EXCLUDED.inc_applicable,
                       iva_applicable = EXCLUDED.iva_applicable,
                       updated_at = now()""",
                tenant_id,
                profile_settings['inc_applicable'],
                profile_settings['iva_applicable'],
            )

    return {'success': True, 'message': 'Datos fiscales actualizados'}


_dian_matias_deps = [
    Depends(require_module(Module.MI_NEGOCIO)),
    Depends(require_matias_dian_capability),
]


@router.get("/dian-resolutions", dependencies=_dian_matias_deps)
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


@router.post("/dian-resolutions", dependencies=_dian_matias_deps)
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

    if not prefix or not resolution_number or from_number <= 0 or to_number <= 0:
        raise HTTPException(status_code=422, detail="Prefijo, número de resolución y rango son requeridos")
    if from_number >= to_number:
        raise HTTPException(status_code=422, detail="El rango 'desde' debe ser menor que 'hasta'")

    # Validate / seed current_number (warocol.com#589). The allocator in
    # api-facturacion uses `next = current_number + 1`, so the correct initial
    # state is `from_number - 1`. Default to that when the client omits the
    # field. Reject any explicit value outside [from_number - 1, to_number] —
    # values below would re-use already-validated DIAN numbers from previous
    # resolutions and collide with Matias' history.
    raw_current = data.get('current_number')
    if raw_current is None:
        current_number = from_number - 1
    else:
        current_number = int(raw_current)
        if current_number < from_number - 1:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"current_number ({current_number}) debe ser >= from_number - 1 "
                    f"({from_number - 1}). Valores menores reusarían números ya emitidos."
                ),
            )
        if current_number > to_number:
            raise HTTPException(
                status_code=422,
                detail=f"current_number ({current_number}) excede to_number ({to_number}).",
            )

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


@router.put("/dian-resolutions/{resolution_id}", dependencies=_dian_matias_deps)
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

    from_number = int(data.get('from_number', 0))
    to_number = int(data.get('to_number', 0))
    if from_number <= 0 or to_number <= 0:
        raise HTTPException(status_code=422, detail="from_number y to_number son requeridos")
    if from_number >= to_number:
        raise HTTPException(status_code=422, detail="El rango 'desde' debe ser menor que 'hasta'")

    # Same current_number validation as POST (warocol.com#589). Updates that
    # set current_number below `from_number - 1` would reactivate the bug
    # this issue fixed.
    raw_current = data.get('current_number')
    if raw_current is None:
        current_number = from_number - 1
    else:
        current_number = int(raw_current)
        if current_number < from_number - 1:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"current_number ({current_number}) debe ser >= from_number - 1 "
                    f"({from_number - 1}). Valores menores reusarían números ya emitidos."
                ),
            )
        if current_number > to_number:
            raise HTTPException(
                status_code=422,
                detail=f"current_number ({current_number}) excede to_number ({to_number}).",
            )

    async with get_db_connection() as conn:
        # warocol.com#592 — Forward-only invariant: current_number can never
        # decrease. The DB trigger `dian_resolutions_no_rewind_trigger` will
        # block it at the storage layer, but we surface a friendlier 422 here
        # before the UPDATE rather than a generic 500 from the trigger.
        existing = await conn.fetchrow(
            "SELECT current_number FROM dian_resolutions WHERE id = $1::uuid AND tenant_id = $2",
            resolution_id, tenant_id,
        )
        if existing is not None and current_number < existing['current_number']:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"current_number ({current_number}) no puede ser menor que el actual "
                    f"({existing['current_number']}). DIAN prohíbe reutilizar números — "
                    "el contador solo avanza."
                ),
            )

        await conn.execute(
            """UPDATE dian_resolutions
               SET resolution_number = $1, prefix = $2, date_from = $3, date_to = $4,
                   from_number = $5, to_number = $6, current_number = $7, document_type = $8
               WHERE id = $9::uuid AND tenant_id = $10""",
            data.get('resolution_number'), data.get('prefix', '').strip().upper(),
            date_from, date_to,
            from_number, to_number,
            current_number, data.get('document_type', 'invoice'),
            resolution_id, tenant_id,
        )

    return {'success': True, 'message': 'Resolución actualizada'}


@router.patch("/dian-resolutions/{resolution_id}/toggle", dependencies=_dian_matias_deps)
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


@router.get("/dian-resolutions/gaps", dependencies=_dian_matias_deps)
async def list_dian_sequence_gaps(
    request: Request,
    resolution_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    """List sequence gaps for the active tenant (warocol.com#592).

    Returns the audit trail of DIAN numbers that were allocated but never
    accepted by Matias. Each row is permanently retired — DIAN forbids
    number reuse. Filterable by resolution.
    """
    from app.core.middleware import require_valid_session
    from app.database import get_db_connection

    session = require_valid_session(request)
    tenant_id = session.tenant_id

    if limit > 200:
        limit = 200

    params = [tenant_id, limit, offset]
    resolution_clause = ""
    if resolution_id:
        resolution_clause = " AND g.resolution_id = $4::uuid"
        params.append(resolution_id)

    query = f"""
        SELECT
            g.id, g.resolution_id, g.prefix, g.skipped_number, g.reason,
            g.matias_response, g.original_attempt_order_id, g.created_at,
            r.resolution_number, r.from_number, r.to_number,
            o.order_number AS original_order_number
        FROM dian_sequence_gaps g
        JOIN dian_resolutions r ON r.id = g.resolution_id
        LEFT JOIN orders o ON o.id = g.original_attempt_order_id
        WHERE g.tenant_id = $1{resolution_clause}
        ORDER BY g.created_at DESC
        LIMIT $2 OFFSET $3
    """

    async with get_db_connection(use_transaction=False) as conn:
        rows = await conn.fetch(query, *params)
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM dian_sequence_gaps WHERE tenant_id = $1",
            tenant_id,
        )

    return {
        'success': True,
        'total': total,
        'limit': limit,
        'offset': offset,
        'data': [
            {
                'id': str(r['id']),
                'resolution_id': str(r['resolution_id']),
                'resolution_number': r['resolution_number'],
                'prefix': r['prefix'],
                'skipped_number': r['skipped_number'],
                'reason': r['reason'],
                'matias_response': r['matias_response'],
                'original_attempt_order_id': (
                    str(r['original_attempt_order_id']) if r['original_attempt_order_id'] else None
                ),
                'original_order_number': r['original_order_number'],
                'from_number': r['from_number'],
                'to_number': r['to_number'],
                'created_at': r['created_at'].isoformat() if r['created_at'] else None,
            }
            for r in rows
        ],
    }


@router.get("/dian-resolutions/gaps-summary", dependencies=_dian_matias_deps)
async def dian_gaps_summary(request: Request):
    """Aggregate gap counts for the active tenant (warocol.com#592).

    Returns counts for the last 24h / 7d / 30d windows. The frontend
    uses this to surface a range-burn alert on the resolutions card.
    """
    from app.core.middleware import require_valid_session
    from app.database import get_db_connection

    session = require_valid_session(request)
    tenant_id = session.tenant_id

    async with get_db_connection(use_transaction=False) as conn:
        row = await conn.fetchrow(
            """SELECT
                   COUNT(*) FILTER (WHERE created_at > now() - interval '24 hours') AS last_24h,
                   COUNT(*) FILTER (WHERE created_at > now() - interval '7 days')   AS last_7d,
                   COUNT(*) FILTER (WHERE created_at > now() - interval '30 days')  AS last_30d,
                   COUNT(*)                                                          AS total
               FROM dian_sequence_gaps
               WHERE tenant_id = $1""",
            tenant_id,
        )

    return {
        'success': True,
        'data': {
            'last_24h': row['last_24h'] or 0,
            'last_7d': row['last_7d'] or 0,
            'last_30d': row['last_30d'] or 0,
            'total': row['total'] or 0,
        },
    }


@router.get("/facturacion-status", dependencies=_dian_matias_deps)
async def get_facturacion_status(request: Request):
    """
    Get Matias API connection status and last emitted document.
    """
    from app.core.middleware import require_valid_session
    from app.database import get_db_connection

    session = require_valid_session(request)
    tenant_id = session.tenant_id

    from app.core.matias_environment import (
        matias_environment_for_tenant,
        matias_environment_label,
    )

    async with get_db_connection() as conn:
        tenant_row = await conn.fetchrow(
            "SELECT slug FROM tenants WHERE id = $1",
            tenant_id,
        )
        last_doc = await conn.fetchrow(
            """SELECT prefix, invoice_number, status, document_type, created_at
               FROM electronic_invoices
               WHERE tenant_id = $1
               ORDER BY created_at DESC
               LIMIT 1""",
            tenant_id,
        )

    tenant_slug = tenant_row['slug'] if tenant_row else None
    environment_id = matias_environment_for_tenant(tenant_id, tenant_slug)

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
            'environment': matias_environment_label(environment_id),
            'environment_id': environment_id,
            'last_document': last_document,
        },
    }


ALLOWED_IMAGE_TYPES = {'image/jpeg', 'image/png', 'image/webp'}
MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5MB


@router.post("/upload-image", dependencies=[Depends(require_module(Module.MI_NEGOCIO))])
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

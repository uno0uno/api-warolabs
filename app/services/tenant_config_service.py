"""
Tenant configuration service - handles tenant public profile management
Requires authentication - these are admin endpoints
"""
import asyncio
import json
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Literal
from uuid import UUID
from fastapi import Request, HTTPException
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError
from app.core.sales_tax_profile import settings_for_sales_tax_profile
from app.core.timezones import DEFAULT_TENANT_TIMEZONE, normalize_timezone, validate_timezone
from app.services.hospitality_tax_packs import ensure_wave1_tax_pack
from app.services.hospitality_tax_jurisdictions import (
    JURISDICTION_COUNTRIES,
    apply_jurisdiction_pack,
    list_jurisdictions,
    normalize_jurisdiction_code,
)
from app.core.tenant_prefs import (
    DEFAULT_CURRENCY_CODE,
    DEFAULT_TENANT_LOCALE,
    normalize_currency_code,
    normalize_locale,
    validate_currency_code,
    validate_locale,
)
from app.models.tenant_public_profile import (
    TenantPublicProfile,
    TenantPublicProfileCreate,
    TenantPublicProfileUpdate,
    TenantPublicProfileResponse,
    ToggleProfileResponse
)
from app.services import public_restaurant_service
from app.services import tenant_financial_profile_service
from app.services.aws_s3_service import AWSS3Service
import logging

logger = logging.getLogger(__name__)


def _profile_from_row(row) -> TenantPublicProfile:
    profile_data = dict(row)
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
    profile_data['timezone'] = normalize_timezone(profile_data.get('timezone'))
    profile_data['locale'] = normalize_locale(profile_data.get('locale'))
    profile_data['currency_code'] = normalize_currency_code(profile_data.get('currency_code'))
    return TenantPublicProfile(**profile_data)


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
                        city, neighborhood, latitude, longitude, timezone,
                        locale, currency_code,
                        business_hours, social_media,
                        seo_title, seo_description,
                        accepts_online_orders, min_order_amount, online_order_max_amount, estimated_preparation_time,
                        tables_enabled,
                        comandas_enabled, kds_enabled,
                        auto_select_generic_enabled,
                        expediter_enabled,
                        minimum_consumption_enabled, minimum_consumption_amount, minimum_consumption_restrictive,
                        updated_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23, $24, $25, $26, $27, $28, $29, $30, $31, $32, $33, CURRENT_TIMESTAMP)
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
                        timezone = EXCLUDED.timezone,
                        locale = EXCLUDED.locale,
                        currency_code = EXCLUDED.currency_code,
                        business_hours = EXCLUDED.business_hours,
                        social_media = EXCLUDED.social_media,
                        seo_title = EXCLUDED.seo_title,
                        seo_description = EXCLUDED.seo_description,
                        accepts_online_orders = EXCLUDED.accepts_online_orders,
                        min_order_amount = EXCLUDED.min_order_amount,
                        online_order_max_amount = EXCLUDED.online_order_max_amount,
                        estimated_preparation_time = EXCLUDED.estimated_preparation_time,
                        tables_enabled = EXCLUDED.tables_enabled,
                        comandas_enabled = EXCLUDED.comandas_enabled,
                        kds_enabled = EXCLUDED.kds_enabled,
                        auto_select_generic_enabled = EXCLUDED.auto_select_generic_enabled,
                        expediter_enabled = EXCLUDED.expediter_enabled,
                        minimum_consumption_enabled = EXCLUDED.minimum_consumption_enabled,
                        minimum_consumption_amount = EXCLUDED.minimum_consumption_amount,
                        minimum_consumption_restrictive = EXCLUDED.minimum_consumption_restrictive,
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
                    profile_data.timezone,
                    profile_data.locale,
                    profile_data.currency_code,
                    json.dumps(profile_data.business_hours) if profile_data.business_hours is not None else None,
                    json.dumps(profile_data.social_media) if profile_data.social_media is not None else None,
                    profile_data.seo_title,
                    profile_data.seo_description,
                    profile_data.accepts_online_orders,
                    profile_data.min_order_amount,
                    profile_data.online_order_max_amount,
                    profile_data.estimated_preparation_time,
                    profile_data.tables_enabled,
                    profile_data.comandas_enabled,
                    profile_data.kds_enabled,
                    profile_data.auto_select_generic_enabled,
                    profile_data.expediter_enabled,
                    profile_data.minimum_consumption_enabled,
                    profile_data.minimum_consumption_amount,
                    profile_data.minimum_consumption_restrictive
                )

                profile = _profile_from_row(result)

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
            exists_query = "SELECT id, comandas_enabled FROM tenant_public_profiles WHERE tenant_id = $1"
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

                # Validate city_slug against the curated catalog before INSERT
                # (warocol.com#615). Same gate as the UPDATE path below.
                if data_dict.get('city_slug'):
                    known = await public_restaurant_service.is_city_slug_known(
                        data_dict['city_slug']
                    )
                    if not known:
                        raise HTTPException(
                            status_code=400,
                            detail=f"city_slug '{data_dict['city_slug']}' is not in the catalog. "
                                   "Pick a city from /public/cities.",
                        )

                insert_query = """
                    INSERT INTO tenant_public_profiles (
                        tenant_id, slug, display_name, is_active,
                        description, logo_url, banner_url,
                        phone_number, email, address,
                        country, city, city_slug, neighborhood,
                        business_hours, social_media, timezone,
                        locale, currency_code,
                        accepts_online_orders, min_order_amount, online_order_max_amount, estimated_preparation_time,
                        is_manually_open,
                        comandas_enabled, kds_enabled,
                        auto_select_generic_enabled,
                        expediter_enabled,
                        minimum_consumption_enabled, minimum_consumption_amount, minimum_consumption_restrictive
                    ) VALUES ($1, $2, $3, FALSE, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23, $24, $25, $26, $27, $28, $29, $30)
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
                    data_dict.get('country', 'Colombia'),
                    data_dict.get('city'),
                    data_dict.get('city_slug'),
                    data_dict.get('neighborhood'),
                    json.dumps(data_dict['business_hours']) if data_dict.get('business_hours') is not None else None,
                    json.dumps(data_dict['social_media']) if data_dict.get('social_media') is not None else None,
                    data_dict.get('timezone', DEFAULT_TENANT_TIMEZONE),
                    data_dict.get('locale', DEFAULT_TENANT_LOCALE),
                    data_dict.get('currency_code', DEFAULT_CURRENCY_CODE),
                    # Default True so new tenants are immediately able to
                    # receive online orders — the platform's core value
                    # prop (warocol.com#626). Operators can still toggle
                    # off from /negocio at any time. Existing rows are
                    # untouched by this change.
                    data_dict.get('accepts_online_orders', True),
                    data_dict.get('min_order_amount', 0),
                    data_dict.get('online_order_max_amount'),
                    data_dict.get('estimated_preparation_time', 30),
                    data_dict.get('is_manually_open', True),
                    data_dict.get('comandas_enabled', False),
                    data_dict.get('kds_enabled', False),
                    data_dict.get('auto_select_generic_enabled', False),
                    data_dict.get('expediter_enabled', False),
                    data_dict.get('minimum_consumption_enabled', False),
                    data_dict.get('minimum_consumption_amount', 0),
                    data_dict.get('minimum_consumption_restrictive', False),
                )

                profile = _profile_from_row(result)
                logger.info(f"Created new public profile for tenant {tenant_id} via PATCH upsert")
                return TenantPublicProfileResponse(success=True, data=profile)

            # Build dynamic update query
            update_fields = []
            params = []
            param_counter = 1

            # Only update fields that were provided
            data_dict = profile_data.model_dump(exclude_unset=True)

            # Cross-field validation: kds_enabled requires comandas_enabled
            if data_dict.get('kds_enabled') is True:
                current_comandas = exists['comandas_enabled'] if exists else False
                new_comandas = data_dict.get('comandas_enabled', current_comandas)
                if not new_comandas:
                    raise HTTPException(
                        status_code=400,
                        detail="kds_enabled requiere que comandas_enabled sea true"
                    )

            # Issue #537 — expediter_enabled also requires comandas_enabled.
            if data_dict.get('expediter_enabled') is True:
                current_comandas = exists['comandas_enabled'] if exists else False
                new_comandas = data_dict.get('comandas_enabled', current_comandas)
                if not new_comandas:
                    raise HTTPException(
                        status_code=400,
                        detail="expediter_enabled requiere que comandas_enabled sea true"
                    )

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

            # Validate city_slug against the curated catalog (warocol.com#615).
            # Operators can only pick from public_cities — free text is rejected
            # at the API boundary so the directory routing stays consistent.
            if data_dict.get('city_slug'):
                known = await public_restaurant_service.is_city_slug_known(
                    data_dict['city_slug']
                )
                if not known:
                    raise HTTPException(
                        status_code=400,
                        detail=f"city_slug '{data_dict['city_slug']}' is not in the catalog. "
                               "Pick a city from /public/cities.",
                    )

            if 'timezone' in data_dict:
                data_dict['timezone'] = validate_timezone(data_dict['timezone'])
            # Explicit null → Colombia defaults; invalid strings → 400 (not 500).
            if 'locale' in data_dict:
                if data_dict['locale'] is None:
                    data_dict['locale'] = DEFAULT_TENANT_LOCALE
                else:
                    try:
                        data_dict['locale'] = validate_locale(data_dict['locale'])
                    except ValueError as exc:
                        raise HTTPException(status_code=400, detail=str(exc)) from exc
            if 'currency_code' in data_dict:
                if data_dict['currency_code'] is None:
                    data_dict['currency_code'] = DEFAULT_CURRENCY_CODE
                else:
                    try:
                        data_dict['currency_code'] = validate_currency_code(
                            data_dict['currency_code']
                        )
                    except ValueError as exc:
                        raise HTTPException(status_code=400, detail=str(exc)) from exc

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

            profile = _profile_from_row(result)

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
                        email_helpers.send_negocio_welcome_email(
                            profile_email,
                            profile_name,
                            tenant_id=str(tenant_id),
                        )
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

            profile = _profile_from_row(result)
            financial = await tenant_financial_profile_service.build_financial_response(
                conn, tenant_id
            )
            # Keep the legacy display field aligned on reads without making
            # tenant_public_profiles a second financial source of truth.
            profile.currency_code = financial.profile.base_currency_code
            profile.country_code = financial.profile.country_code
            profile.base_currency_code = financial.profile.base_currency_code
            profile.is_currently_open = public_restaurant_service.is_currently_open(
                profile.business_hours,
                profile.is_manually_open,
                profile.timezone,
            )

            return TenantPublicProfileResponse(
                success=True,
                data=profile,
                financial=financial,
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


def decode_tax_config_jsonb(data: Mapping[str, Any]) -> Dict[str, Any]:
    """Decode asyncpg jsonb strings so clients get objects, not raw JSON text."""
    out = dict(data)
    for key in ("tax_lines", "category_map", "menu_category_line_map"):
        val = out.get(key)
        if isinstance(val, str):
            try:
                out[key] = json.loads(val)
            except (TypeError, ValueError):
                pass
    exempt = out.get("exempt_menu_category_ids")
    if isinstance(exempt, str):
        try:
            exempt = json.loads(exempt)
        except (TypeError, ValueError):
            exempt = None
    if isinstance(exempt, (list, tuple)):
        out["exempt_menu_category_ids"] = [str(x) for x in exempt if x is not None]
    elif exempt is None:
        out.setdefault("exempt_menu_category_ids", [])
    return out


def validate_tax_matrix_payload(
    tax_lines: Optional[List[Any]],
    category_map: Optional[Mapping[str, Any]],
    menu_category_line_map: Optional[Mapping[str, Any]] = None,
    exempt_menu_category_ids: Optional[List[Any]] = None,
) -> None:
    """Validate commercial matrix rates and category/menu map line references."""
    line_keys: set[str] = set()
    if tax_lines is not None:
        if not isinstance(tax_lines, list):
            raise HTTPException(status_code=400, detail="tax_lines must be a list")
        for item in tax_lines:
            if not isinstance(item, Mapping):
                raise HTTPException(status_code=400, detail="each tax line must be an object")
            key = str(item.get("key") or "").strip()
            if not key:
                raise HTTPException(status_code=400, detail="each tax line requires a key")
            try:
                rate = float(item.get("rate") if item.get("rate") is not None else 0)
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=400, detail=f"invalid rate for tax line '{key}'"
                ) from exc
            if rate < 0:
                raise HTTPException(
                    status_code=400, detail=f"tax line '{key}' rate must be >= 0"
                )
            line_keys.add(key)

    if category_map is not None:
        if not isinstance(category_map, Mapping):
            raise HTTPException(status_code=400, detail="category_map must be an object")
        if tax_lines is not None:
            for cat, ref in category_map.items():
                if ref in (None, "", "null"):
                    continue
                ref_key = str(ref)
                if ref_key not in line_keys:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"category_map['{cat}'] references unknown tax line key "
                            f"'{ref_key}'"
                        ),
                    )

    if menu_category_line_map is not None:
        if not isinstance(menu_category_line_map, Mapping):
            raise HTTPException(
                status_code=400, detail="menu_category_line_map must be an object"
            )
        for cat_id, ref in menu_category_line_map.items():
            cat_str = str(cat_id).strip()
            if not cat_str:
                raise HTTPException(
                    status_code=400,
                    detail="menu_category_line_map keys must be category UUIDs",
                )
            try:
                UUID(cat_str)
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"menu_category_line_map key '{cat_str}' is not a valid UUID",
                ) from exc
            if ref in (None, "", "null"):
                continue
            if tax_lines is not None:
                ref_key = str(ref)
                if ref_key not in line_keys:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"menu_category_line_map['{cat_str}'] references unknown "
                            f"tax line key '{ref_key}'"
                        ),
                    )

    if exempt_menu_category_ids is not None:
        if not isinstance(exempt_menu_category_ids, list):
            raise HTTPException(
                status_code=400, detail="exempt_menu_category_ids must be a list"
            )
        for item in exempt_menu_category_ids:
            try:
                UUID(str(item))
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"exempt_menu_category_ids entry '{item}' is not a valid UUID",
                ) from exc


def _encode_menu_category_line_map(raw: Optional[Mapping[str, Any]]) -> Optional[str]:
    if raw is None:
        return None
    out: Dict[str, Optional[str]] = {}
    for key, value in raw.items():
        out[str(key)] = None if value in (None, "", "null") else str(value)
    return json.dumps(out)


def _encode_exempt_menu_category_ids(raw: Optional[List[Any]]) -> Optional[List[UUID]]:
    if raw is None:
        return None
    return [UUID(str(x)) for x in raw]

def validate_co_rate_fields(data: Any) -> None:
    """Validate optional CO column rates when present on TaxConfigUpdate."""
    for field in ("iva_rate", "inc_rate", "liquor_tax_rate"):
        raw = getattr(data, field, None)
        if raw is None:
            continue
        try:
            rate = Decimal(str(raw))
        except Exception as exc:
            raise HTTPException(
                status_code=400, detail=f"{field} must be a number >= 0"
            ) from exc
        if rate < 0:
            raise HTTPException(status_code=400, detail=f"{field} must be >= 0")


def _optional_co_rate(data: Any, field: str):
    raw = getattr(data, field, None)
    return None if raw is None else Decimal(str(raw))


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

            profile = await conn.fetchrow(
                "SELECT country_code FROM tenant_financial_profiles WHERE tenant_id = $1",
                tenant_id,
            )
            if profile and profile.get("country_code"):
                applied = await ensure_wave1_tax_pack(
                    conn, tenant_id, profile["country_code"]
                )
                if applied:
                    row = await conn.fetchrow(
                        "SELECT * FROM tenant_tax_config WHERE tenant_id = $1",
                        tenant_id,
                    )

            return {"success": True, "data": decode_tax_config_jsonb(dict(row))}

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error fetching tax config: {e}")
        raise HTTPException(status_code=500, detail=f"Error fetching tax config: {str(e)}")


async def get_tax_jurisdictions(request: Request, country: str) -> dict:
    """Return static US state or CA province tax jurisdiction catalog."""
    try:
        require_valid_session(request)
        code = (country or "").strip().upper()
        if code not in JURISDICTION_COUNTRIES:
            raise HTTPException(
                status_code=400,
                detail="country must be US or CA",
            )
        return {"success": True, "data": list_jurisdictions(code)}
    except AuthenticationError as e:
        raise e
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing tax jurisdictions: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Error listing tax jurisdictions: {str(e)}",
        )


async def update_tax_config(request: Request, data) -> dict:
    """
    Upsert the sale-tax configuration for the active tenant.

    This controls which taxes are calculated on sales. Issuer identity fields
    such as type_organization_id and tax_regime_id are stored via fiscal-data.
    Accepts a TaxConfigUpdate-shaped object.
    """
    try:
        session = require_valid_session(request)
        tenant_id = session.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        tax_lines = getattr(data, "tax_lines", None)
        category_map = getattr(data, "category_map", None)
        menu_category_line_map = getattr(data, "menu_category_line_map", None)
        exempt_menu_category_ids = getattr(data, "exempt_menu_category_ids", None)
        validate_tax_matrix_payload(
            tax_lines,
            category_map,
            menu_category_line_map,
            exempt_menu_category_ids,
        )
        validate_co_rate_fields(data)
        iva_rate = _optional_co_rate(data, "iva_rate")
        inc_rate = _optional_co_rate(data, "inc_rate")
        liquor_tax_rate = _optional_co_rate(data, "liquor_tax_rate")
        menu_map_json = _encode_menu_category_line_map(menu_category_line_map)
        exempt_ids_pg = _encode_exempt_menu_category_ids(exempt_menu_category_ids)

        async with get_db_connection() as conn:
            fiscal_row = await conn.fetchrow(
                """SELECT sales_tax_profile
                   FROM tenant_fiscal_data
                   WHERE tenant_id = $1""",
                tenant_id,
            )
            sales_tax_profile = (
                fiscal_row['sales_tax_profile']
                if fiscal_row and fiscal_row['sales_tax_profile']
                else 'unconfigured'
            )
            expected = settings_for_sales_tax_profile(sales_tax_profile)
            if expected is None:
                if data.inc_applicable or data.iva_applicable:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            'Selecciona primero el perfil tributario de ventas '
                            'en Datos fiscales'
                        ),
                    )
            elif (
                data.inc_applicable is not expected['inc_applicable']
                or data.iva_applicable is not expected['iva_applicable']
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        'La selección de IVA/INC no coincide con el perfil '
                        'tributario configurado'
                    ),
                )

            profile = await conn.fetchrow(
                "SELECT country_code FROM tenant_financial_profiles WHERE tenant_id = $1",
                tenant_id,
            )
            country_code = (profile["country_code"] if profile else None) or ""
            jurisdiction_raw = getattr(data, "tax_jurisdiction_code", None)
            if (
                jurisdiction_raw is not None
                and str(jurisdiction_raw).strip() != ""
                and country_code.upper() in JURISDICTION_COUNTRIES
            ):
                try:
                    jurisdiction = normalize_jurisdiction_code(
                        country_code, jurisdiction_raw
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                applied, row = await apply_jurisdiction_pack(
                    conn, tenant_id, country_code, jurisdiction
                )
                if not applied or not row:
                    raise HTTPException(
                        status_code=400,
                        detail="Could not apply tax jurisdiction pack",
                    )
                # Allow commercial rate override on top of jurisdiction seed.
                if tax_lines is not None or menu_map_json is not None or exempt_ids_pg is not None:
                    commercial_flag = getattr(data, "commercial_tax_applicable", None)
                    row = await conn.fetchrow(
                        """
                        UPDATE tenant_tax_config
                        SET tax_lines = COALESCE($2::jsonb, tax_lines),
                            category_map = COALESCE($3::jsonb, category_map),
                            commercial_tax_applicable = COALESCE($4, commercial_tax_applicable),
                            menu_category_line_map = COALESCE($5::jsonb, menu_category_line_map),
                            exempt_menu_category_ids = COALESCE($6::uuid[], exempt_menu_category_ids),
                            updated_at = NOW()
                        WHERE tenant_id = $1
                        RETURNING *
                        """,
                        tenant_id,
                        json.dumps(tax_lines) if tax_lines is not None else None,
                        json.dumps(category_map) if category_map is not None else None,
                        commercial_flag,
                        menu_map_json,
                        exempt_ids_pg,
                    )
                elif getattr(data, "commercial_tax_applicable", None) is not None:
                    row = await conn.fetchrow(
                        """
                        UPDATE tenant_tax_config
                        SET commercial_tax_applicable = $2,
                            updated_at = NOW()
                        WHERE tenant_id = $1
                        RETURNING *
                        """,
                        tenant_id,
                        data.commercial_tax_applicable,
                    )
                return {"success": True, "data": decode_tax_config_jsonb(dict(row))}

            commercial_flag = getattr(data, "commercial_tax_applicable", None)
            row = await conn.fetchrow(
                """
                INSERT INTO tenant_tax_config (
                    tenant_id,
                    inc_applicable, inc_included_in_price,
                    iva_applicable, iva_included_in_price,
                    liquor_tax_applicable,
                    inc_gl_account_id, iva_gl_account_id, liquor_tax_gl_account_id,
                    tax_lines, category_map, tax_jurisdiction_code,
                    commercial_tax_applicable,
                    iva_rate, inc_rate, liquor_tax_rate,
                    menu_category_line_map, exempt_menu_category_ids
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11::jsonb, $12,
                    COALESCE($13, false),
                    COALESCE($14, 0.19), COALESCE($15, 0.08), COALESCE($16, 0.05),
                    COALESCE($17::jsonb, '{}'::jsonb),
                    COALESCE($18::uuid[], '{}'::uuid[])
                )
                ON CONFLICT (tenant_id) DO UPDATE SET
                    inc_applicable        = EXCLUDED.inc_applicable,
                    inc_included_in_price = EXCLUDED.inc_included_in_price,
                    iva_applicable        = EXCLUDED.iva_applicable,
                    iva_included_in_price = EXCLUDED.iva_included_in_price,
                    liquor_tax_applicable = EXCLUDED.liquor_tax_applicable,
                    inc_gl_account_id = COALESCE(EXCLUDED.inc_gl_account_id, tenant_tax_config.inc_gl_account_id),
                    iva_gl_account_id = COALESCE(EXCLUDED.iva_gl_account_id, tenant_tax_config.iva_gl_account_id),
                    liquor_tax_gl_account_id = COALESCE(EXCLUDED.liquor_tax_gl_account_id, tenant_tax_config.liquor_tax_gl_account_id),
                    tax_lines = COALESCE(EXCLUDED.tax_lines, tenant_tax_config.tax_lines),
                    category_map = COALESCE(EXCLUDED.category_map, tenant_tax_config.category_map),
                    tax_jurisdiction_code = COALESCE(
                        EXCLUDED.tax_jurisdiction_code,
                        tenant_tax_config.tax_jurisdiction_code
                    ),
                    commercial_tax_applicable = COALESCE(
                        EXCLUDED.commercial_tax_applicable,
                        tenant_tax_config.commercial_tax_applicable
                    ),
                    iva_rate = COALESCE($14, tenant_tax_config.iva_rate),
                    inc_rate = COALESCE($15, tenant_tax_config.inc_rate),
                    liquor_tax_rate = COALESCE($16, tenant_tax_config.liquor_tax_rate),
                    menu_category_line_map = COALESCE(
                        $17::jsonb,
                        tenant_tax_config.menu_category_line_map
                    ),
                    exempt_menu_category_ids = COALESCE(
                        $18::uuid[],
                        tenant_tax_config.exempt_menu_category_ids
                    ),
                    updated_at            = NOW()
                RETURNING *
                """,
                tenant_id,
                data.inc_applicable,
                data.inc_included_in_price,
                data.iva_applicable,
                data.iva_included_in_price,
                data.liquor_tax_applicable,
                data.inc_gl_account_id,
                data.iva_gl_account_id,
                data.liquor_tax_gl_account_id,
                json.dumps(tax_lines) if tax_lines is not None else None,
                json.dumps(category_map) if category_map is not None else None,
                None,
                commercial_flag,
                iva_rate,
                inc_rate,
                liquor_tax_rate,
                menu_map_json,
                exempt_ids_pg,
            )

            return {"success": True, "data": decode_tax_config_jsonb(dict(row))}

    except AuthenticationError as e:
        raise e
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating tax config: {e}")
        raise HTTPException(status_code=500, detail=f"Error updating tax config: {str(e)}")

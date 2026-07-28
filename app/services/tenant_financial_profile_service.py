"""Authoritative tenant financial profile and safe country/currency mutation."""
from typing import Any, Optional, Tuple

from fastapi import HTTPException, Request

from app.core.exceptions import AuthenticationError
from app.core.middleware import require_valid_session
from app.core.tenant_prefs import (
    COUNTRY_CURRENCY_PAIRS,
    SUPPORTED_CURRENCY_MINOR_UNITS,
    validate_country_currency_pair,
)
from app.core.timezones import seed_tenant_timezone_from_country
from app.database import get_db_connection
from app.models.tenant_financial_profile import (
    CountryCurrencyOption,
    CurrencyMetadata,
    FinancialCapabilities,
    FinancialEligibility,
    TenantFinancialProfile,
    TenantFinancialProfileResponse,
    TenantFinancialProfileUpdate,
)
from app.services.hospitality_tax_jurisdictions import (
    JURISDICTION_COUNTRIES,
    apply_jurisdiction_pack,
    normalize_jurisdiction_code,
)

PERMANENT_REASON = "PERMANENT_FINANCIAL_ACTIVITY"
TEMPORARY_REASON = "TEMPORARY_OPERATIONAL_ACTIVITY"
CONFIGURED_REASON = "FINANCIAL_PROFILE_CONFIGURED"

_DEFAULT_PROFILE_INSERT = """
    INSERT INTO tenant_financial_profiles (
        tenant_id, country_code, base_currency_code,
        accounting_localization, document_mode, fiscal_provider
    )
    SELECT id, 'CO', 'COP', 'WARO_CO_PUC_V1', 'fiscal_integrated', 'matias'
    FROM tenants
    WHERE id = $1
    ON CONFLICT (tenant_id) DO NOTHING
"""

_BLOCKERS_QUERY = """
    SELECT
        (
            EXISTS (
                SELECT 1 FROM orders
                WHERE tenant_id = $1 AND status = 'completed'
            )
            OR EXISTS (SELECT 1 FROM order_payments WHERE tenant_id = $1)
            OR EXISTS (
                SELECT 1 FROM tenant_journal_entries
                WHERE tenant_id = $1 AND status IN ('posted', 'voided')
            )
            OR EXISTS (SELECT 1 FROM electronic_invoices WHERE tenant_id = $1)
            OR EXISTS (SELECT 1 FROM customer_wallet_movements WHERE tenant_id = $1)
        ) AS permanent_activity,
        (
            EXISTS (
                SELECT 1 FROM orders
                WHERE tenant_id = $1 AND status = 'pending'
            )
            OR EXISTS (
                SELECT 1 FROM table_sessions
                WHERE tenant_id = $1 AND closed_at IS NULL
            )
            OR EXISTS (
                SELECT 1 FROM pos_carts
                WHERE tenant_id = $1 AND status = 'active'
            )
            OR EXISTS (
                SELECT 1 FROM cash_shift_openings
                WHERE tenant_id = $1 AND status = 'open'
            )
        ) AS temporary_activity
"""


def _financial_mode(country_code: str) -> Tuple[str, str, Optional[str]]:
    if country_code == "CO":
        return "WARO_CO_PUC_V1", "fiscal_integrated", "matias"
    return "WARO_HOSPITALITY_GLOBAL_V1", "waro_commercial", None


def _capabilities(country_code: str) -> FinancialCapabilities:
    colombia = country_code == "CO"
    return FinancialCapabilities(
        colombia_puc=colombia,
        colombia_payroll=colombia,
        matias_dian=colombia,
        cop_wallet=colombia,
        wompi=colombia,
        fixed_cop_discounts=colombia,
    )


def _catalog() -> list[CountryCurrencyOption]:
    return [
        CountryCurrencyOption(country_code=country, currency_codes=list(currencies))
        for country, currencies in COUNTRY_CURRENCY_PAIRS.items()
    ]


def _currencies() -> list[CurrencyMetadata]:
    return [
        CurrencyMetadata(currency_code=code, minor_units=minor_units)
        for code, minor_units in sorted(SUPPORTED_CURRENCY_MINOR_UNITS.items())
    ]


def _eligibility(blockers: Any) -> FinancialEligibility:
    permanent = bool(blockers and blockers.get("permanent_activity"))
    temporary = bool(blockers and blockers.get("temporary_activity"))
    if permanent:
        return FinancialEligibility(
            eligible=False,
            lock_type="permanent",
            reason_codes=[PERMANENT_REASON],
        )
    if temporary:
        return FinancialEligibility(
            eligible=False,
            lock_type="temporary",
            reason_codes=[TEMPORARY_REASON],
        )
    return FinancialEligibility(eligible=True, lock_type="none", reason_codes=[])


def _response(profile_row: Any, blockers: Any) -> TenantFinancialProfileResponse:
    profile = TenantFinancialProfile(**dict(profile_row))
    return TenantFinancialProfileResponse(
        profile=profile,
        catalog=_catalog(),
        currencies=_currencies(),
        capabilities=_capabilities(profile.country_code),
        eligibility=_eligibility(blockers),
    )


async def seed_tenant_accounts(conn, tenant_id) -> None:
    """Idempotent chart seed from tenant_financial_profiles.accounting_localization."""
    await conn.execute("SELECT seed_tenant_accounts($1)", tenant_id)


async def build_financial_response(
    conn, tenant_id, *, lock_tenant: bool = False
) -> TenantFinancialProfileResponse:
    """Read/create a tenant profile and calculate non-sensitive lock state."""
    if lock_tenant:
        tenant = await conn.fetchrow(
            "SELECT id FROM tenants WHERE id = $1 FOR UPDATE", tenant_id
        )
        if not tenant:
            raise HTTPException(status_code=404, detail="Tenant not found")

    insert_status = await conn.execute(_DEFAULT_PROFILE_INSERT, tenant_id)
    if insert_status == "INSERT 0 1":
        await seed_tenant_accounts(conn, tenant_id)
    suffix = " FOR UPDATE" if lock_tenant else ""
    profile = await conn.fetchrow(
        """
        SELECT tenant_id, country_code, base_currency_code,
               accounting_localization, document_mode, fiscal_provider,
               selection_revision, created_at, updated_at
        FROM tenant_financial_profiles
        WHERE tenant_id = $1
        """ + suffix,
        tenant_id,
    )
    if not profile:
        raise HTTPException(status_code=404, detail="Tenant not found")
    blockers = await conn.fetchrow(_BLOCKERS_QUERY, tenant_id)
    return _response(profile, blockers)


async def get_financial_profile(request: Request) -> TenantFinancialProfileResponse:
    session = require_valid_session(request)
    if not session.tenant_id:
        raise AuthenticationError("Tenant ID is required")
    async with get_db_connection() as conn:
        return await build_financial_response(conn, session.tenant_id)


async def update_financial_profile(
    request: Request, data: TenantFinancialProfileUpdate
) -> TenantFinancialProfileResponse:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    country_code, currency_code = validate_country_currency_pair(
        data.country_code, data.base_currency_code
    )

    async with get_db_connection() as conn:
        async with conn.transaction():
            current = await build_financial_response(conn, tenant_id, lock_tenant=True)
            same_pair = (
                current.profile.country_code == country_code
                and current.profile.base_currency_code == currency_code
            )
            if same_pair:
                jurisdiction = data.tax_jurisdiction_code
                if country_code in JURISDICTION_COUNTRIES and jurisdiction:
                    try:
                        jurisdiction = normalize_jurisdiction_code(
                            country_code, jurisdiction
                        )
                    except ValueError as exc:
                        raise HTTPException(status_code=422, detail=str(exc)) from exc
                    applied, _ = await apply_jurisdiction_pack(
                        conn, tenant_id, country_code, jurisdiction
                    )
                    if not applied:
                        raise HTTPException(
                            status_code=422,
                            detail=(
                                f"Unsupported jurisdiction {jurisdiction} "
                                f"for {country_code}"
                            ),
                        )
                    await seed_tenant_timezone_from_country(
                        conn, tenant_id, country_code
                    )
                    return await build_financial_response(conn, tenant_id)
                return current

            # Country/currency are immutable after first configure (onboarding /
            # default profile). Stronger than activity-only eligibility locks.
            lock_type = (
                current.eligibility.lock_type
                if not current.eligibility.eligible
                else "configured"
            )
            reason_codes = (
                current.eligibility.reason_codes
                if not current.eligibility.eligible
                else [CONFIGURED_REASON]
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "FINANCIAL_PROFILE_LOCKED",
                    "lock_type": lock_type,
                    "reason_codes": reason_codes,
                },
            )

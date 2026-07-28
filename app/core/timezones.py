"""Tenant operational timezone helpers.

Operational tenant time controls local business dates, schedules, and report
boundaries. Fiscal/legal Colombia time should use a separate named helper.
"""
from datetime import date, datetime, time, timedelta, timezone
from inspect import isawaitable
import logging
from typing import Optional, Tuple, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg


DEFAULT_TENANT_TIMEZONE = "America/Bogota"
logger = logging.getLogger(__name__)

# Primary IANA default per hospitality HQ country (v1). Multi-zone countries
# use one primary; Negocio may override later. warocol.com#1854 / epic #1852.
COUNTRY_DEFAULT_TIMEZONES: dict[str, str] = {
    "CO": "America/Bogota",
    "PA": "America/Panama",
    "CL": "America/Santiago",
    "US": "America/New_York",
    "CA": "America/Toronto",
    "DO": "America/Santo_Domingo",
    "UY": "America/Montevideo",
    "AU": "Australia/Sydney",
    "NZ": "Pacific/Auckland",
    "SG": "Asia/Singapore",
    "AE": "Asia/Dubai",
}


def default_timezone_for_country(country_code: Optional[str]) -> str:
    """Return the primary operational timezone for a catalog country."""
    code = str(country_code or "").strip().upper()
    mapped = COUNTRY_DEFAULT_TIMEZONES.get(code)
    if mapped:
        return mapped
    return DEFAULT_TENANT_TIMEZONE


async def seed_tenant_timezone_from_country(conn, tenant_id, country_code: str) -> str:
    """Seed public-profile timezone from country when still on the global default.

    Does not overwrite a user/Negocio override (any timezone other than the
    legacy DEFAULT_TENANT_TIMEZONE). Creates a minimal public profile row when
    missing (slug + display_name from tenants).
    """
    timezone_name = default_timezone_for_country(country_code)
    updated = await conn.execute(
        """
        UPDATE tenant_public_profiles
        SET timezone = $2
        WHERE tenant_id = $1
          AND (
            timezone IS NULL
            OR btrim(timezone) = ''
            OR timezone = $3
          )
        """,
        tenant_id,
        timezone_name,
        DEFAULT_TENANT_TIMEZONE,
    )
    if updated and str(updated).endswith("1"):
        return timezone_name

    tenant = await conn.fetchrow(
        "SELECT slug, name FROM tenants WHERE id = $1",
        tenant_id,
    )
    if not tenant or not tenant.get("slug"):
        return timezone_name

    await conn.execute(
        """
        INSERT INTO tenant_public_profiles (tenant_id, slug, display_name, timezone)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (tenant_id) DO UPDATE
        SET timezone = EXCLUDED.timezone
        WHERE tenant_public_profiles.timezone IS NULL
           OR btrim(tenant_public_profiles.timezone) = ''
           OR tenant_public_profiles.timezone = $5
        """,
        tenant_id,
        tenant["slug"],
        tenant.get("name") or tenant["slug"],
        timezone_name,
        DEFAULT_TENANT_TIMEZONE,
    )
    return timezone_name


def validate_timezone(value: Optional[str]) -> str:
    """Return a normalized IANA timezone or raise ValueError for user input."""
    if value is None or not isinstance(value, str) or not value.strip():
        raise ValueError("timezone must be a valid IANA timezone")

    timezone_name = value.strip()
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone must be a valid IANA timezone") from exc
    return timezone_name


def normalize_timezone(value: Optional[str]) -> str:
    """Return a safe tenant timezone, falling back for legacy/invalid values."""
    try:
        return validate_timezone(value)
    except ValueError:
        return DEFAULT_TENANT_TIMEZONE


def get_zoneinfo(value: Optional[str]) -> ZoneInfo:
    """Return ZoneInfo for a tenant timezone, using the safe default if needed."""
    return ZoneInfo(normalize_timezone(value))


def local_date_for_tenant(value: Union[datetime, date], timezone_name: Optional[str]) -> date:
    """Return a tenant-local date while preserving legacy naive date handling."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(get_zoneinfo(timezone_name)).date()
        return value.date()
    return value


async def resolve_tenant_timezone(conn, tenant_id) -> str:
    """Resolve a tenant's operational timezone from profile config."""
    try:
        result = conn.fetchval(
            "SELECT timezone FROM tenant_public_profiles WHERE tenant_id = $1",
            tenant_id,
        )
        value = await result if isawaitable(result) else result
    except asyncpg.UndefinedColumnError:
        logger.warning(
            "tenant_public_profiles.timezone missing; using default timezone. "
            "Apply sql/20260626_tenant_timezone.sql."
        )
        return DEFAULT_TENANT_TIMEZONE
    return normalize_timezone(value)


def tenant_today(value: Optional[str], now: Optional[datetime] = None) -> date:
    """Return today's local date in the tenant operational timezone."""
    zone = get_zoneinfo(value)
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(zone).date()


def local_day_utc_range(
    day: date,
    value: Optional[str],
) -> Tuple[datetime, datetime]:
    """Return [start, end) UTC datetimes for a tenant-local calendar day."""
    zone = get_zoneinfo(value)
    local_start = datetime.combine(day, time.min, tzinfo=zone)
    local_end = local_start + timedelta(days=1)
    return (
        local_start.astimezone(timezone.utc),
        local_end.astimezone(timezone.utc),
    )

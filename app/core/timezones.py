"""Tenant operational timezone helpers.

Operational tenant time controls local business dates, schedules, and report
boundaries. Fiscal/legal Colombia time should use a separate named helper.
"""
from datetime import date, datetime, time, timedelta, timezone
from inspect import isawaitable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TENANT_TIMEZONE = "America/Bogota"


def validate_timezone(value: str | None) -> str:
    """Return a normalized IANA timezone or raise ValueError for user input."""
    if value is None or not isinstance(value, str) or not value.strip():
        raise ValueError("timezone must be a valid IANA timezone")

    timezone_name = value.strip()
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone must be a valid IANA timezone") from exc
    return timezone_name


def normalize_timezone(value: str | None) -> str:
    """Return a safe tenant timezone, falling back for legacy/invalid values."""
    try:
        return validate_timezone(value)
    except ValueError:
        return DEFAULT_TENANT_TIMEZONE


def get_zoneinfo(value: str | None) -> ZoneInfo:
    """Return ZoneInfo for a tenant timezone, using the safe default if needed."""
    return ZoneInfo(normalize_timezone(value))


async def resolve_tenant_timezone(conn, tenant_id) -> str:
    """Resolve a tenant's operational timezone from profile config."""
    result = conn.fetchval(
        "SELECT timezone FROM tenant_public_profiles WHERE tenant_id = $1",
        tenant_id,
    )
    value = await result if isawaitable(result) else result
    return normalize_timezone(value)


def tenant_today(value: str | None, now: datetime | None = None) -> date:
    """Return today's local date in the tenant operational timezone."""
    zone = get_zoneinfo(value)
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(zone).date()


def local_day_utc_range(
    day: date,
    value: str | None,
) -> tuple[datetime, datetime]:
    """Return [start, end) UTC datetimes for a tenant-local calendar day."""
    zone = get_zoneinfo(value)
    local_start = datetime.combine(day, time.min, tzinfo=zone)
    local_end = local_start + timedelta(days=1)
    return (
        local_start.astimezone(timezone.utc),
        local_end.astimezone(timezone.utc),
    )

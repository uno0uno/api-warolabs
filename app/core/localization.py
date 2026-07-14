"""Backend localization helpers for tenant-facing receipts."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import gettext
from inspect import isawaitable
import logging
from pathlib import Path
from typing import Any, Optional

import asyncpg
from babel.dates import format_datetime as babel_format_datetime
from babel.numbers import format_decimal

from app.core.tenant_prefs import (
    SUPPORTED_CURRENCY_CODES,
    currency_minor_units,
)
from app.core.timezones import DEFAULT_TENANT_TIMEZONE, get_zoneinfo, normalize_timezone

logger = logging.getLogger(__name__)

DEFAULT_LOCALE = "es"
DEFAULT_CURRENCY = "COP"
SUPPORTED_LOCALES = {"es", "en"}
SUPPORTED_CURRENCIES = SUPPORTED_CURRENCY_CODES
LOCALES_DIR = Path(__file__).resolve().parents[1] / "locales"


@dataclass(frozen=True)
class TenantLocaleSettings:
    locale: str = DEFAULT_LOCALE
    currency_code: str = DEFAULT_CURRENCY
    timezone: str = DEFAULT_TENANT_TIMEZONE


def normalize_locale(value: Optional[str]) -> str:
    """Return a supported two-letter locale, falling back to Spanish."""
    if not isinstance(value, str):
        return DEFAULT_LOCALE
    normalized = value.strip().lower().replace("_", "-").split("-", 1)[0]
    return normalized if normalized in SUPPORTED_LOCALES else DEFAULT_LOCALE


def normalize_currency(value: Optional[str]) -> str:
    """Return a supported base-currency code."""
    if not isinstance(value, str):
        return DEFAULT_CURRENCY
    normalized = value.strip().upper()
    return normalized if normalized in SUPPORTED_CURRENCIES else DEFAULT_CURRENCY


def babel_locale(locale: Optional[str]) -> str:
    return "en_US" if normalize_locale(locale) == "en" else "es_CO"


def get_translator(locale: Optional[str]):
    """Return gettext translator for app/locales with safe fallback."""
    lang = normalize_locale(locale)
    return gettext.translation(
        "messages",
        localedir=str(LOCALES_DIR),
        languages=[lang],
        fallback=True,
    ).gettext


async def resolve_tenant_locale_settings(conn, tenant_id: Any) -> TenantLocaleSettings:
    """Resolve locale/timezone plus authoritative tenant base currency."""
    if not tenant_id:
        return TenantLocaleSettings()
    try:
        result = conn.fetchrow(
            """
            SELECT
                tpp.locale,
                COALESCE(tfp.base_currency_code, tpp.currency_code, 'COP') AS currency_code,
                tpp.timezone
            FROM tenants t
            LEFT JOIN tenant_public_profiles tpp ON tpp.tenant_id = t.id
            LEFT JOIN tenant_financial_profiles tfp ON tfp.tenant_id = t.id
            WHERE t.id = $1
            """,
            tenant_id,
        )
        row = await result if isawaitable(result) else result
    except (asyncpg.UndefinedColumnError, asyncpg.UndefinedTableError):
        logger.warning(
            "Tenant locale/financial profile schema missing; using CO/COP defaults. "
            "Apply migrations 099 and 103."
        )
        return TenantLocaleSettings()
    except Exception as exc:
        logger.warning("Could not resolve tenant locale settings for %s: %s", tenant_id, exc)
        return TenantLocaleSettings()

    if not row:
        return TenantLocaleSettings()
    return TenantLocaleSettings(
        locale=normalize_locale(row.get("locale")),
        currency_code=normalize_currency(row.get("currency_code")),
        timezone=normalize_timezone(row.get("timezone")),
    )


def format_money(amount: Any, locale: Optional[str] = None, currency: Optional[str] = None) -> str:
    """Format money for receipts while keeping COP semantics."""
    code = normalize_currency(currency)
    lang = normalize_locale(locale)
    try:
        value = Decimal(str(amount or 0))
    except Exception:
        value = Decimal("0")
    number = format_decimal(value, format="#,##0", locale=babel_locale(lang))
    if lang == "en":
        return f"{code} {number}"
    if code == "COP":
        return f"${number}"
    return f"{code} {number}"


def get_currency_minor_units(currency: Optional[str]) -> int:
    """Expose supported ISO minor units to localization consumers."""
    return currency_minor_units(currency)


def format_datetime(value: datetime, locale: Optional[str] = None, timezone_name: Optional[str] = None) -> str:
    """Format a datetime in the tenant timezone with Babel."""
    zone = get_zoneinfo(timezone_name)
    instant = value
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    local = instant.astimezone(zone)
    pattern = "MMMM d, y, h:mm a" if normalize_locale(locale) == "en" else "d 'de' MMMM 'de' y, h:mm a"
    text = babel_format_datetime(local, pattern, locale=babel_locale(locale), tzinfo=zone)
    return text.replace("\u202f", " ").replace("\xa0", " ")

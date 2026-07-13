from datetime import datetime, timezone

import asyncpg
import pytest

from app.core.localization import (
    DEFAULT_CURRENCY,
    DEFAULT_LOCALE,
    format_datetime,
    format_money,
    normalize_currency,
    normalize_locale,
    resolve_tenant_locale_settings,
)


class FakeConn:
    def __init__(self, row=None, error=None):
        self.row = row
        self.error = error

    async def fetchrow(self, *_args, **_kwargs):
        if self.error:
            raise self.error
        return self.row


def test_normalize_locale_and_currency_fallbacks():
    assert normalize_locale("en-US") == "en"
    assert normalize_locale("es_CO") == "es"
    assert normalize_locale("fr") == DEFAULT_LOCALE
    assert normalize_currency("cop") == "COP"
    assert normalize_currency("USD") == DEFAULT_CURRENCY


@pytest.mark.asyncio
async def test_resolve_tenant_locale_settings_uses_profile_values():
    settings = await resolve_tenant_locale_settings(
        FakeConn({"locale": "en", "currency_code": "COP", "timezone": "America/Bogota"}),
        "tenant-1",
    )
    assert settings.locale == "en"
    assert settings.currency_code == "COP"
    assert settings.timezone == "America/Bogota"


@pytest.mark.asyncio
async def test_resolve_tenant_locale_settings_falls_back_for_invalid_or_missing_columns():
    settings = await resolve_tenant_locale_settings(
        FakeConn({"locale": "legacy", "currency_code": "USD", "timezone": "Bad/Zone"}),
        "tenant-1",
    )
    assert settings.locale == "es"
    assert settings.currency_code == "COP"
    assert settings.timezone == "America/Bogota"

    missing = await resolve_tenant_locale_settings(
        FakeConn(error=asyncpg.UndefinedColumnError("missing locale")),
        "tenant-1",
    )
    assert missing.locale == "es"
    assert missing.currency_code == "COP"
    assert missing.timezone == "America/Bogota"


def test_babel_formatters_for_receipts():
    dt = datetime(2026, 7, 12, 1, 51, tzinfo=timezone.utc)
    assert format_money(373200, "en", "COP") == "COP 373,200"
    assert format_money(373200, "es", "COP") == "$373.200"
    assert format_datetime(dt, "en", "America/Bogota") == "July 11, 2026, 8:51 PM"
    assert format_datetime(dt, "es", "America/Bogota") == "11 de julio de 2026, 8:51 p. m."

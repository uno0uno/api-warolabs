from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.core.timezones import (
    DEFAULT_TENANT_TIMEZONE,
    local_date_for_tenant,
    local_day_utc_range,
    normalize_timezone,
    resolve_tenant_timezone,
    tenant_today,
)
from app.models.tenant_public_profile import TenantPublicProfileUpdate
from app.services import pos_context_service, tenant_config_service


def test_normalize_timezone_falls_back_for_missing_or_invalid_values():
    assert normalize_timezone(None) == DEFAULT_TENANT_TIMEZONE
    assert normalize_timezone("") == DEFAULT_TENANT_TIMEZONE
    assert normalize_timezone("Not/AZone") == DEFAULT_TENANT_TIMEZONE
    assert normalize_timezone("America/Mexico_City") == "America/Mexico_City"


def test_tenant_today_and_local_day_range_use_tenant_timezone():
    now = datetime(2026, 1, 2, 4, 30, tzinfo=timezone.utc)

    assert tenant_today("America/Bogota", now=now) == date(2026, 1, 1)
    assert tenant_today("America/Mexico_City", now=now) == date(2026, 1, 1)

    start_utc, end_utc = local_day_utc_range(date(2026, 1, 1), "America/Bogota")
    assert start_utc.isoformat() == "2026-01-01T05:00:00+00:00"
    assert end_utc.isoformat() == "2026-01-02T05:00:00+00:00"


def test_local_date_for_tenant_preserves_naive_and_converts_aware_datetimes():
    aware = datetime(2026, 6, 7, 0, 30, tzinfo=timezone.utc)
    naive = datetime(2026, 6, 7, 0, 30)

    assert local_date_for_tenant(aware, "Europe/Madrid") == date(2026, 6, 7)
    assert local_date_for_tenant(aware, "America/Bogota") == date(2026, 6, 6)
    assert local_date_for_tenant(naive, "America/Bogota") == date(2026, 6, 7)
    assert local_date_for_tenant(date(2026, 6, 7), "America/Bogota") == date(2026, 6, 7)


def test_public_profile_write_model_rejects_invalid_timezone():
    with pytest.raises(PydanticValidationError):
        TenantPublicProfileUpdate(timezone="Not/AZone")

    model = TenantPublicProfileUpdate(timezone="America/Mexico_City")
    assert model.timezone == "America/Mexico_City"


def test_profile_from_row_normalizes_legacy_invalid_timezone():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    profile = tenant_config_service._profile_from_row({
        "id": uuid4(),
        "tenant_id": uuid4(),
        "slug": "demo",
        "display_name": "Demo",
        "is_active": True,
        "timezone": "Legacy/Bad",
        "created_at": now,
        "updated_at": now,
    })

    assert profile.timezone == DEFAULT_TENANT_TIMEZONE


@pytest.mark.asyncio
async def test_resolve_tenant_timezone_normalizes_profile_value():
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value="America/Mexico_City")

    assert await resolve_tenant_timezone(conn, uuid4()) == "America/Mexico_City"


@pytest.mark.asyncio
async def test_resolve_tenant_timezone_falls_back_for_missing_or_legacy_value():
    conn = AsyncMock()
    conn.fetchval = AsyncMock(side_effect=[None, "Legacy/Bad"])

    assert await resolve_tenant_timezone(conn, uuid4()) == DEFAULT_TENANT_TIMEZONE
    assert await resolve_tenant_timezone(conn, uuid4()) == DEFAULT_TENANT_TIMEZONE


def _pos_context_row(timezone_name):
    return {
        "display_name": "Demo",
        "timezone": timezone_name,
        "kds_enabled": None,
        "comandas_enabled": None,
        "expediter_enabled": None,
        "tables_enabled": None,
        "table_qr_module_enabled": None,
        "accepts_online_orders": None,
        "auto_select_generic_enabled": None,
        "open_sale_enabled": None,
        "minimum_consumption_enabled": None,
        "minimum_consumption_amount": None,
        "minimum_consumption_restrictive": None,
        "waiter_attribution_enabled": None,
        "tables_label_singular": None,
        "tables_label_plural": None,
        "tip_enabled": None,
        "tip_taxable_default": None,
        "tip_default_percentages": None,
        "tip_preselect_index": None,
        "logo_url": None,
        "allow_promo_line_opt_out": None,
        "promo_conflict_strategy": None,
        "promo_type_block_map": None,
        "nit": None,
        "business_name": None,
        "type_organization_id": None,
        "tax_regime_id": None,
        "tax_level_id": None,
        "fiscal_address": None,
        "city": None,
        "city_id": None,
        "fiscal_phone": None,
        "fiscal_email": None,
        "receipt_document_label": None,
        "receipt_tip_label": None,
        "show_logo_on_receipts": None,
        "inc_applicable": None,
        "inc_rate": None,
        "inc_included_in_price": None,
        "iva_applicable": None,
        "iva_rate": None,
        "iva_included_in_price": None,
        "liquor_tax_applicable": None,
    }


@pytest.mark.asyncio
async def test_pos_context_exposes_normalized_timezone():
    tenant_id = UUID("93b3e582-34fa-44a6-8d0f-bf82a3608727")

    @asynccontextmanager
    async def _ctx(*_, **__):
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value=_pos_context_row("Bad/Legacy"))
        conn.fetch = AsyncMock(return_value=[])
        yield conn

    with patch("app.services.pos_context_service.get_db_connection", side_effect=_ctx), \
         patch("app.services.pos_context_service.get_readiness", new=AsyncMock(return_value={"ready": False})), \
         patch("app.services.pos_context_service.fetch_open_sale_product", new=AsyncMock(return_value=None)):
        payload = await pos_context_service.get_restaurant_context(tenant_id)

    assert payload["timezone"] == DEFAULT_TENANT_TIMEZONE

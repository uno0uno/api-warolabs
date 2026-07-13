from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import asyncpg

from app.core.tenant_prefs import normalize_ui_locale, validate_ui_locale
from app.services.operaciones_context_service import update_ui_locale
from app.services.pos_context_service import (
    _CONTEXT_QUERY,
    _CONTEXT_QUERY_WITHOUT_UI_LOCALE,
    _CONTEXT_QUERY_WITHOUT_PREFS,
    get_restaurant_context,
)
from app.services.tenants_service import (
    _USER_TENANTS_QUERY,
    _USER_TENANTS_QUERY_WITHOUT_UI_LOCALE,
    get_user_tenants,
)


def test_ui_locale_validation_is_independent_and_complete():
    for code in ("es", "en", "pt", "fr", "de", "hi", "zh", "ar"):
        assert validate_ui_locale(code.upper()) == code
    assert normalize_ui_locale("xx") == "es"
    with pytest.raises(ValueError):
        validate_ui_locale("xx")


def test_context_and_tenant_queries_have_legacy_fallbacks():
    assert "tpp.ui_locale" in _CONTEXT_QUERY
    assert "NULL AS ui_locale" in _CONTEXT_QUERY_WITHOUT_UI_LOCALE
    assert "tpp.timezone" in _CONTEXT_QUERY_WITHOUT_UI_LOCALE
    assert "tpp.locale" in _CONTEXT_QUERY_WITHOUT_UI_LOCALE
    assert "tpp.currency_code" in _CONTEXT_QUERY_WITHOUT_UI_LOCALE
    assert "NULL AS ui_locale" in _CONTEXT_QUERY_WITHOUT_PREFS
    assert "tpp.ui_locale" in _USER_TENANTS_QUERY
    assert "NULL AS ui_locale" in _USER_TENANTS_QUERY_WITHOUT_UI_LOCALE
    assert "tenant_public_profiles tpp" not in _USER_TENANTS_QUERY_WITHOUT_UI_LOCALE


def test_migration_backfills_existing_tenant_locale_before_not_null():
    sql = Path("migrations/100_tenant_ui_locale.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS ui_locale TEXT;" in sql
    assert "WHEN lower(locale) IN ('es', 'en') THEN lower(locale)" in sql
    assert sql.index("SET ui_locale = CASE") < sql.index("ALTER COLUMN ui_locale SET NOT NULL")


@pytest.mark.asyncio
async def test_update_ui_locale_scopes_upsert_to_tenant():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"ui_locale": "fr"})

    @asynccontextmanager
    async def db_context(*_args, **_kwargs):
        yield conn

    with patch(
        "app.services.operaciones_context_service.get_db_connection",
        side_effect=db_context,
    ):
        result = await update_ui_locale(tenant_id, "FR")

    assert result == {"success": True, "data": {"ui_locale": "fr"}}
    query, passed_tenant_id, locale = conn.fetchrow.await_args.args
    assert "WHERE t.id = $1" in query
    assert passed_tenant_id == tenant_id
    assert locale == "fr"


@pytest.mark.asyncio
async def test_user_tenant_list_exposes_normalized_ui_locale():
    user_id = uuid4()
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[{
        "id": uuid4(),
        "name": "Demo",
        "slug": "demo",
        "ui_locale": "DE",
    }])

    @asynccontextmanager
    async def db_context(*_args, **_kwargs):
        yield conn

    session = SimpleNamespace(user_id=user_id)
    with patch(
        "app.services.tenants_service.require_valid_session",
        return_value=session,
    ), patch(
        "app.services.tenants_service.get_db_connection",
        side_effect=db_context,
    ):
        response = await get_user_tenants(MagicMock())

    assert response.data[0].ui_locale == "de"
    assert conn.fetch.await_args.args[1] == user_id


@pytest.mark.asyncio
async def test_user_tenant_list_recovers_without_ui_locale_column():
    user_id = uuid4()
    row = {"id": uuid4(), "name": "Demo", "slug": "demo", "ui_locale": None}
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=[
        asyncpg.UndefinedColumnError("ui_locale missing"),
        [row],
    ])

    @asynccontextmanager
    async def db_context(*_args, **_kwargs):
        yield conn

    session = SimpleNamespace(user_id=user_id)
    with patch(
        "app.services.tenants_service.require_valid_session",
        return_value=session,
    ), patch(
        "app.services.tenants_service.get_db_connection",
        side_effect=db_context,
    ) as get_connection:
        response = await get_user_tenants(MagicMock())

    assert response.data[0].ui_locale == "es"
    assert conn.fetch.await_count == 2
    assert conn.fetch.await_args_list[1].args[0] == _USER_TENANTS_QUERY_WITHOUT_UI_LOCALE
    get_connection.assert_called_once_with(use_transaction=False)


@pytest.mark.asyncio
async def test_pos_context_first_fallback_preserves_existing_preferences():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[
        asyncpg.UndefinedColumnError("ui_locale missing"),
        None,
    ])

    @asynccontextmanager
    async def db_context(*_args, **_kwargs):
        yield conn

    with patch(
        "app.services.pos_context_service.get_db_connection",
        side_effect=db_context,
    ):
        result = await get_restaurant_context(uuid4())

    assert result is None
    assert conn.fetchrow.await_args_list[1].args[0] == _CONTEXT_QUERY_WITHOUT_UI_LOCALE


@pytest.mark.asyncio
async def test_pos_context_uses_general_fallback_for_older_missing_prefs():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[
        asyncpg.UndefinedColumnError("ui_locale missing"),
        asyncpg.UndefinedColumnError("locale missing"),
        None,
    ])

    @asynccontextmanager
    async def db_context(*_args, **_kwargs):
        yield conn

    with patch(
        "app.services.pos_context_service.get_db_connection",
        side_effect=db_context,
    ):
        result = await get_restaurant_context(uuid4())

    assert result is None
    assert conn.fetchrow.await_args_list[2].args[0] == _CONTEXT_QUERY_WITHOUT_PREFS

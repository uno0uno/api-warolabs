"""Bitácora CUD writers for menu / supply / team / integrations / negocio (warocol.com#2327)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import Request

from app.services import (
    api_tokens_service,
    categories_service,
    inventory_service,
    tenant_config_service,
    tenants_service,
    warehouse_categories_service,
)


class _AsyncContext:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, extra, tb):
        return False


def _session(tenant_id, user_id):
    return SimpleNamespace(tenant_id=tenant_id, user_id=user_id)


def _capture():
    recorded = []

    async def capture_record(conn, tid, **kwargs):
        recorded.append({"tenant_id": tid, **kwargs})

    return recorded, capture_record


@pytest.mark.asyncio
async def test_revoke_api_token_records_integraciones_event():
    tenant_id = uuid4()
    user_id = uuid4()
    token_id = str(uuid4())
    recorded, capture_record = _capture()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"role": "admin"})
    conn.execute = AsyncMock(return_value="UPDATE 1")

    with patch("app.services.api_tokens_service.get_session_from_request", new=AsyncMock(return_value={
        "user_id": str(user_id),
        "tenant_id": str(tenant_id),
    })), \
         patch("app.services.api_tokens_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.api_tokens_service.record_module_event", new=capture_record):
        result = await api_tokens_service.revoke_api_token(Request({"type": "http"}), token_id)

    assert result["success"] is True
    assert len(recorded) == 1
    assert recorded[0]["domain"] == "integraciones"
    assert recorded[0]["action"] == "api_token_revoked"


@pytest.mark.asyncio
async def test_list_api_tokens_does_not_record():
    tenant_id = uuid4()
    recorded, capture_record = _capture()
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])

    with patch("app.services.api_tokens_service.get_session_from_request", new=AsyncMock(return_value={
        "user_id": str(uuid4()),
        "tenant_id": str(tenant_id),
    })), \
         patch("app.services.api_tokens_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.api_tokens_service.record_module_event", new=capture_record):
        result = await api_tokens_service.list_api_tokens(Request({"type": "http"}))

    assert result["success"] is True
    assert recorded == []


@pytest.mark.asyncio
async def test_delete_tenant_member_records_equipo_event():
    tenant_id = uuid4()
    user_id = uuid4()
    member_id = str(uuid4())
    target_user = uuid4()
    recorded, capture_record = _capture()
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value="admin")
    conn.fetchrow = AsyncMock(return_value={
        "id": member_id,
        "user_id": target_user,
        "name": "Luis",
        "email": "luis@example.com",
    })
    conn.execute = AsyncMock()

    with patch("app.services.tenants_service.require_valid_session", return_value=_session(tenant_id, user_id)), \
         patch("app.services.tenants_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.tenants_service.record_module_event", new=capture_record):
        result = await tenants_service.delete_tenant_member(Request({"type": "http"}), member_id)

    assert result.success is True
    assert len(recorded) == 1
    assert recorded[0]["domain"] == "equipo"
    assert recorded[0]["action"] == "member_deleted"


@pytest.mark.asyncio
async def test_archive_warehouse_category_records_abastecimiento_event():
    tenant_id = uuid4()
    category_id = uuid4()
    recorded, capture_record = _capture()
    conn = AsyncMock()
    owned = {
        "id": category_id,
        "tenant_id": tenant_id,
        "name": "Lácteos",
        "normalized_name": "lacteos",
        "is_active": False,
        "ingredient_count": 0,
        "global_count": 0,
        "tenant_count": 0,
    }
    recorded_fn = capture_record

    with patch("app.services.warehouse_categories_service._load_owned_category", new=AsyncMock(return_value=owned)), \
         patch("app.services.warehouse_categories_service._load_visible_category", new=AsyncMock(return_value=owned)), \
         patch("app.services.warehouse_categories_service.record_module_event", new=recorded_fn):
        conn.execute = AsyncMock()
        result = await warehouse_categories_service.archive_warehouse_category(conn, tenant_id, category_id)

    assert result["id"] == category_id
    assert len(recorded) == 1
    assert recorded[0]["domain"] == "abastecimiento"
    assert recorded[0]["action"] == "warehouse_category_archived"


@pytest.mark.asyncio
async def test_list_online_menu_categories_does_not_record():
    tenant_id = uuid4()
    recorded, capture_record = _capture()
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])

    with patch("app.services.categories_service.require_valid_session", return_value=_session(tenant_id, uuid4())), \
         patch("app.services.categories_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.categories_service.record_module_event", new=capture_record):
        result = await categories_service.list_online_menu_categories(Request({"type": "http"}))

    assert result["success"] is True
    assert recorded == []


@pytest.mark.asyncio
async def test_get_inventory_stock_does_not_record():
    tenant_id = uuid4()
    recorded, capture_record = _capture()

    with patch("app.services.inventory_service.require_valid_session", return_value=_session(tenant_id, uuid4())), \
         patch("app.services.inventory_service._get_inventory_stock_for_tenant", new=AsyncMock(return_value={"success": True})), \
         patch("app.services.inventory_service.record_module_event", new=capture_record):
        result = await inventory_service.get_inventory_stock(Request({"type": "http"}), MagicMock())

    assert result["success"] is True
    assert recorded == []


@pytest.mark.asyncio
async def test_get_own_public_profile_does_not_record():
    tenant_id = uuid4()
    recorded, capture_record = _capture()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)

    with patch("app.services.tenant_config_service.require_valid_session", return_value=_session(tenant_id, uuid4())), \
         patch("app.services.tenant_config_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.tenant_config_service.record_module_event", new=capture_record):
        result = await tenant_config_service.get_own_public_profile(Request({"type": "http"}))

    assert result is None
    assert recorded == []

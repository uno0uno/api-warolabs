from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.services.operaciones_context_service import ALLOWED_TOGGLES, update_toggle


@pytest.mark.asyncio
async def test_floor_canvas_enabled_is_whitelisted():
    assert "floor_canvas_enabled" in ALLOWED_TOGGLES


@pytest.mark.asyncio
async def test_update_toggle_persists_floor_canvas():
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _ctx():
        yield conn

    tenant_id = uuid4()
    conn = MagicMock()
    conn.execute = AsyncMock()

    with patch(
        "app.services.operaciones_context_service.get_db_connection", side_effect=lambda: _ctx()
    ):
        await update_toggle(tenant_id, "floor_canvas_enabled", True)

    query = conn.execute.await_args.args[0]
    assert "floor_canvas_enabled" in query
    assert "ON CONFLICT (tenant_id) DO UPDATE" in query
    assert conn.execute.await_args.args[1:] == (tenant_id, True)


@pytest.mark.asyncio
async def test_toggle_endpoint_wires_floor_canvas():
    from app.routers import operaciones_context as router_mod

    tenant_id = uuid4()
    request = SimpleNamespace()
    body = SimpleNamespace(enabled=True)

    with (
        patch.object(
            router_mod, "require_valid_session", return_value=SimpleNamespace(tenant_id=tenant_id)
        ),
        patch.object(router_mod, "update_toggle", new=AsyncMock(return_value={"ok": True})) as mocked,
    ):
        result = await router_mod.toggle_floor_canvas(request, body)

    mocked.assert_awaited_once_with(tenant_id, "floor_canvas_enabled", True)
    assert result == {"ok": True}


def test_model_defaults_floor_canvas_off():
    from app.models.tenant_public_profile import TenantPublicProfile

    assert TenantPublicProfile.model_fields["floor_canvas_enabled"].default is False

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.services.operaciones_context_service import ALLOWED_POS_TABLES_LAYOUTS, update_pos_tables_layout


def test_allowed_tables_layouts():
    assert ALLOWED_POS_TABLES_LAYOUTS == frozenset({"grid", "list", "canvas"})


@pytest.mark.asyncio
async def test_update_pos_tables_layout_persists():
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _ctx():
        yield conn

    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"pos_tables_layout_default": "canvas"})

    with patch(
        "app.services.operaciones_context_service.get_db_connection", side_effect=lambda: _ctx()
    ):
        result = await update_pos_tables_layout(tenant_id, "canvas")

    query = conn.fetchrow.await_args.args[0]
    assert "pos_tables_layout_default" in query
    assert "ON CONFLICT (tenant_id) DO UPDATE" in query
    assert result["data"] == {"pos_tables_layout_default": "canvas"}


@pytest.mark.asyncio
async def test_update_pos_tables_layout_rejects_unknown():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await update_pos_tables_layout(uuid4(), "carousel")

    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_tables_layout_request_validates():
    from app.routers.operaciones_context import PosTablesLayoutRequest

    assert PosTablesLayoutRequest(layout=" grid ").layout == "grid"
    with pytest.raises(Exception):
        PosTablesLayoutRequest(layout="carousel")

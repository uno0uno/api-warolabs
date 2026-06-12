"""Tests for DELETE /cierre/open-shift/{opening_id}."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.core.exceptions import APIError
from app.services.cierre_service import delete_open_shift


def _open_row(opening_id=None, status="open", accounting_period_id=None):
    return {
        "id": opening_id or uuid4(),
        "status": status,
        "accounting_period_id": accounting_period_id,
    }


@pytest.mark.asyncio
async def test_delete_open_shift_happy_path():
    tenant_id = uuid4()
    opening_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=_open_row(opening_id=opening_id))
    conn.execute = AsyncMock()

    @asynccontextmanager
    async def db_ctx(**_kwargs):
        yield conn

    with patch(
        "app.services.cierre_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id),
    ), patch(
        "app.services.cierre_service.get_db_connection",
        side_effect=db_ctx,
    ):
        result = await delete_open_shift(AsyncMock(), opening_id)

    assert result == {"success": True, "data": None}
    conn.execute.assert_awaited_once()
    args = conn.execute.await_args.args
    assert "DELETE FROM cash_shift_openings" in args[0]
    assert args[1] == opening_id
    assert args[2] == tenant_id


@pytest.mark.asyncio
async def test_delete_open_shift_not_found():
    tenant_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)

    @asynccontextmanager
    async def db_ctx(**_kwargs):
        yield conn

    with patch(
        "app.services.cierre_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id),
    ), patch(
        "app.services.cierre_service.get_db_connection",
        side_effect=db_ctx,
    ):
        with pytest.raises(APIError) as exc:
            await delete_open_shift(AsyncMock(), uuid4())

    assert exc.value.status_code == 404
    assert "Apertura no encontrada" in exc.value.message


@pytest.mark.asyncio
async def test_delete_open_shift_rejects_closed():
    tenant_id = uuid4()
    opening_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        return_value=_open_row(
            opening_id=opening_id,
            status="closed",
            accounting_period_id=uuid4(),
        ),
    )

    @asynccontextmanager
    async def db_ctx(**_kwargs):
        yield conn

    with patch(
        "app.services.cierre_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id),
    ), patch(
        "app.services.cierre_service.get_db_connection",
        side_effect=db_ctx,
    ):
        with pytest.raises(APIError) as exc:
            await delete_open_shift(AsyncMock(), opening_id)

    assert exc.value.status_code == 409
    conn.execute.assert_not_awaited()

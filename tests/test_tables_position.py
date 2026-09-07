from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.core.exceptions import APIError, NotFoundError
from app.services import tables_service


def _db_context(conn):
    @asynccontextmanager
    async def _ctx():
        yield conn

    return _ctx


def _session(tenant_id=None):
    return SimpleNamespace(user_id=uuid4(), tenant_id=tenant_id or uuid4())


def _position_row(table_id, pos_x=10.5, pos_y=20.5, zona="terraza"):
    return {
        "id": table_id,
        "name": "Mesa 1",
        "code": "M1",
        "capacity": 4,
        "display_order": 1,
        "pos_x": pos_x,
        "pos_y": pos_y,
        "zona": zona,
        "status": "free",
        "is_active": True,
        "is_bar": False,
        "qr_enabled": False,
        "qr_public_token": None,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }


@pytest.mark.asyncio
async def test_update_table_position_persists_single_write():
    tenant_id = uuid4()
    table_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=_position_row(table_id))

    with (
        patch("app.services.tables_service.require_valid_session", return_value=_session(tenant_id)),
        patch("app.services.tables_service.get_db_connection", side_effect=_db_context(conn)),
    ):
        result = await tables_service.update_table_position(
            object(), table_id, pos_x=10.5, pos_y=20.5, zona="terraza"
        )

    query = conn.fetchrow.await_args.args[0]
    assert "UPDATE tables" in query
    assert "pos_x = $3" in query
    assert conn.fetchrow.await_args.args[3:] == (10.5, 20.5, "terraza")
    assert result["data"]["pos_x"] == 10.5
    assert result["data"]["pos_y"] == 20.5
    assert result["data"]["zona"] == "terraza"


@pytest.mark.asyncio
async def test_update_table_position_not_found_for_cross_tenant():
    tenant_id = uuid4()
    table_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)

    with (
        patch("app.services.tables_service.require_valid_session", return_value=_session(tenant_id)),
        patch("app.services.tables_service.get_db_connection", side_effect=_db_context(conn)),
    ):
        with pytest.raises(NotFoundError):
            await tables_service.update_table_position(object(), table_id, pos_x=1.0)


@pytest.mark.asyncio
async def test_update_table_position_rejects_long_zona():
    with patch("app.services.tables_service.require_valid_session", return_value=_session()):
        with pytest.raises(APIError) as exc:
            await tables_service.update_table_position(object(), uuid4(), zona="z" * 51)

    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_list_tables_selects_and_formats_positions():
    tenant_id = uuid4()
    table_id = uuid4()
    conn = MagicMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction.return_value = tx
    row = _position_row(table_id)
    row.update(
        {
            "assigned_member_id": None,
            "assigned_member_name": None,
            "assigned_member_role": None,
            "session_id": None,
            "last_closed_at": None,
            "last_closed_session_id": None,
            "effective_waiter_member_id": None,
            "effective_waiter_member_name": None,
            "effective_waiter_member_role": None,
        }
    )
    conn.fetch = AsyncMock(return_value=[row])

    with (
        patch("app.services.tables_service.require_valid_session", return_value=_session(tenant_id)),
        patch("app.services.tables_service.get_db_connection", side_effect=_db_context(conn)),
        patch("app.services.tables_service._ensure_bar_table", new=AsyncMock()),
    ):
        result = await tables_service.list_tables(object())

    query = conn.fetch.await_args.args[0]
    assert "t.pos_x" in query
    assert "t.pos_y" in query
    assert "t.zona" in query
    assert result["data"][0]["pos_x"] == 10.5
    assert result["data"][0]["zona"] == "terraza"

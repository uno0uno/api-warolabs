from contextlib import asynccontextmanager
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


def _wall_row(wall_id, zona="Salon"):
    return {
        "id": wall_id,
        "zona": zona,
        "x1": 0.0,
        "y1": 0.0,
        "x2": 4.0,
        "y2": 0.0,
    }


@pytest.mark.asyncio
async def test_list_floor_walls_returns_tenant_walls():
    tenant_id = uuid4()
    wall_id = uuid4()
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[_wall_row(wall_id)])

    with (
        patch("app.services.tables_service.require_valid_session", return_value=_session(tenant_id)),
        patch("app.services.tables_service.get_db_connection", side_effect=_db_context(conn)),
    ):
        result = await tables_service.list_floor_walls(object())

    query = conn.fetch.await_args.args[0]
    assert "FROM floor_walls" in query
    assert "tenant_id = $1" in query
    assert conn.fetch.await_args.args[1] == tenant_id
    assert result["data"][0]["id"] == str(wall_id)
    assert result["data"][0]["x2"] == 4.0


@pytest.mark.asyncio
async def test_create_floor_wall_inserts_segment():
    tenant_id = uuid4()
    wall_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=_wall_row(wall_id))

    with (
        patch("app.services.tables_service.require_valid_session", return_value=_session(tenant_id)),
        patch("app.services.tables_service.get_db_connection", side_effect=_db_context(conn)),
    ):
        result = await tables_service.create_floor_wall(
            object(), {"zona": "Salon", "x1": 0, "y1": 0, "x2": 4, "y2": 0}
        )

    query = conn.fetchrow.await_args.args[0]
    assert "INSERT INTO floor_walls" in query
    assert result["data"]["zona"] == "Salon"


@pytest.mark.asyncio
async def test_create_floor_wall_rejects_bad_coords_or_zona():
    with patch("app.services.tables_service.require_valid_session", return_value=_session()):
        with pytest.raises(APIError) as exc:
            await tables_service.create_floor_wall(
                object(), {"zona": "Salon", "x1": float("nan"), "y1": 0, "x2": 1, "y2": 1}
            )
        assert exc.value.status_code == 400

        with pytest.raises(APIError) as exc2:
            await tables_service.create_floor_wall(
                object(), {"zona": "  ", "x1": 0, "y1": 0, "x2": 1, "y2": 1}
            )
        assert exc2.value.status_code == 400


@pytest.mark.asyncio
async def test_delete_floor_wall_scoped_and_404():
    tenant_id = uuid4()
    wall_id = uuid4()
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=wall_id)

    with (
        patch("app.services.tables_service.require_valid_session", return_value=_session(tenant_id)),
        patch("app.services.tables_service.get_db_connection", side_effect=_db_context(conn)),
    ):
        result = await tables_service.delete_floor_wall(object(), wall_id)

    query = conn.fetchval.await_args.args[0]
    assert "DELETE FROM floor_walls" in query
    assert "tenant_id = $2" in query
    assert result["data"]["id"] == str(wall_id)

    conn.fetchval = AsyncMock(return_value=None)
    with (
        patch("app.services.tables_service.require_valid_session", return_value=_session(tenant_id)),
        patch("app.services.tables_service.get_db_connection", side_effect=_db_context(conn)),
    ):
        with pytest.raises(NotFoundError):
            await tables_service.delete_floor_wall(object(), wall_id)


@pytest.mark.asyncio
async def test_update_floor_wall_partial_move():
    tenant_id = uuid4()
    wall_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=_wall_row(wall_id))

    with (
        patch("app.services.tables_service.require_valid_session", return_value=_session(tenant_id)),
        patch("app.services.tables_service.get_db_connection", side_effect=_db_context(conn)),
    ):
        result = await tables_service.update_floor_wall(object(), wall_id, {"x2": 8.0})

    query = conn.fetchrow.await_args.args[0]
    assert "UPDATE floor_walls" in query
    assert "x2 = $3" in query
    assert "tenant_id = $2" in query
    assert result["data"]["id"] == str(wall_id)


@pytest.mark.asyncio
async def test_update_floor_wall_404_and_400():
    tenant_id = uuid4()
    wall_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)

    with (
        patch("app.services.tables_service.require_valid_session", return_value=_session(tenant_id)),
        patch("app.services.tables_service.get_db_connection", side_effect=_db_context(conn)),
    ):
        with pytest.raises(NotFoundError):
            await tables_service.update_floor_wall(object(), wall_id, {"x1": 1.0})

    with patch("app.services.tables_service.require_valid_session", return_value=_session()):
        with pytest.raises(APIError) as exc:
            await tables_service.update_floor_wall(object(), wall_id, {"y1": float("inf")})
        assert exc.value.status_code == 400

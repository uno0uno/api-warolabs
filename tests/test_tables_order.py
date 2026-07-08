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


def _tx():
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    return tx


def _session(tenant_id=None):
    return SimpleNamespace(user_id=uuid4(), tenant_id=tenant_id or uuid4())


def _table_row(table_id, name, *, is_bar=False, display_order=None):
    return {
        "id": table_id,
        "name": name,
        "code": "BAR" if is_bar else name.replace("Mesa ", "M"),
        "capacity": None if is_bar else 4,
        "display_order": display_order,
        "status": "open" if is_bar else "free",
        "is_active": True,
        "is_bar": is_bar,
        "qr_enabled": False,
        "qr_public_token": None,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
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


@pytest.mark.asyncio
async def test_list_tables_orders_by_display_order_with_name_fallback():
    tenant_id = uuid4()
    first_id = uuid4()
    second_id = uuid4()
    conn = MagicMock()
    conn.transaction.return_value = _tx()
    conn.fetch = AsyncMock(return_value=[
        _table_row(first_id, "Mesa 10", display_order=1),
        _table_row(second_id, "Mesa 2", display_order=2),
    ])

    with (
        patch("app.services.tables_service.require_valid_session", return_value=_session(tenant_id)),
        patch("app.services.tables_service.get_db_connection", side_effect=_db_context(conn)),
        patch("app.services.tables_service._ensure_bar_table", new=AsyncMock()),
    ):
        result = await tables_service.list_tables(object())

    query = conn.fetch.await_args.args[0]
    assert "t.display_order" in query
    assert "ORDER BY t.is_bar DESC, t.is_active DESC, t.display_order NULLS LAST, t.name" in query
    assert [row["id"] for row in result["data"]] == [str(first_id), str(second_id)]
    assert result["data"][0]["display_order"] == 1


@pytest.mark.asyncio
async def test_reorder_tables_rejects_duplicate_ids():
    table_id = uuid4()

    with patch("app.services.tables_service.require_valid_session", return_value=_session()):
        with pytest.raises(APIError) as exc:
            await tables_service.reorder_tables(object(), [table_id, table_id])

    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_reorder_tables_rejects_bar_table():
    tenant_id = uuid4()
    table_id = uuid4()
    conn = MagicMock()
    conn.transaction.return_value = _tx()
    conn.fetch = AsyncMock(return_value=[
        {"id": table_id, "is_bar": True, "deleted_at": None},
    ])

    with (
        patch("app.services.tables_service.require_valid_session", return_value=_session(tenant_id)),
        patch("app.services.tables_service.get_db_connection", side_effect=_db_context(conn)),
    ):
        with pytest.raises(APIError) as exc:
            await tables_service.reorder_tables(object(), [table_id])

    assert exc.value.status_code == 409
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_reorder_tables_rejects_missing_or_cross_tenant_id():
    tenant_id = uuid4()
    table_id = uuid4()
    conn = MagicMock()
    conn.transaction.return_value = _tx()
    conn.fetch = AsyncMock(return_value=[])

    with (
        patch("app.services.tables_service.require_valid_session", return_value=_session(tenant_id)),
        patch("app.services.tables_service.get_db_connection", side_effect=_db_context(conn)),
    ):
        with pytest.raises(NotFoundError):
            await tables_service.reorder_tables(object(), [table_id])

    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_reorder_tables_rejects_deleted_table():
    tenant_id = uuid4()
    table_id = uuid4()
    conn = MagicMock()
    conn.transaction.return_value = _tx()
    conn.fetch = AsyncMock(return_value=[
        {"id": table_id, "is_bar": False, "deleted_at": datetime(2026, 1, 1, tzinfo=timezone.utc)},
    ])

    with (
        patch("app.services.tables_service.require_valid_session", return_value=_session(tenant_id)),
        patch("app.services.tables_service.get_db_connection", side_effect=_db_context(conn)),
    ):
        with pytest.raises(NotFoundError):
            await tables_service.reorder_tables(object(), [table_id])

    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_reorder_tables_persists_submitted_regular_order():
    tenant_id = uuid4()
    first_id = uuid4()
    second_id = uuid4()
    conn = MagicMock()
    conn.transaction.return_value = _tx()
    conn.fetch = AsyncMock(return_value=[
        {"id": first_id, "is_bar": False, "deleted_at": None},
        {"id": second_id, "is_bar": False, "deleted_at": None},
    ])
    conn.execute = AsyncMock()

    with (
        patch("app.services.tables_service.require_valid_session", return_value=_session(tenant_id)),
        patch("app.services.tables_service.get_db_connection", side_effect=_db_context(conn)),
    ):
        result = await tables_service.reorder_tables(object(), [first_id, second_id])

    assert result["data"]["table_ids"] == [str(first_id), str(second_id)]
    assert conn.execute.await_count == 2
    update_args = conn.execute.await_args_list[1].args
    assert update_args[2] == [first_id, second_id]


@pytest.mark.asyncio
async def test_create_table_assigns_tail_display_order():
    tenant_id = uuid4()
    table_id = uuid4()
    conn = MagicMock()
    conn.transaction.return_value = _tx()
    conn.fetchval = AsyncMock(side_effect=[7, False])
    conn.fetchrow = AsyncMock(return_value={
        "id": table_id,
        "name": "Mesa 7",
        "code": "M7",
        "capacity": 4,
        "status": "free",
        "is_active": True,
        "is_bar": False,
        "qr_enabled": False,
        "qr_public_token": None,
        "display_order": 7,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    })

    with (
        patch("app.services.tables_service.require_valid_session", return_value=_session(tenant_id)),
        patch("app.services.tables_service.get_db_connection", side_effect=_db_context(conn)),
        patch("app.services.tables_service._resolve_table_code", new=AsyncMock(return_value="M7")),
        patch("app.services.tables_service.check_plan_quota_growth", new=AsyncMock()),
    ):
        result = await tables_service.create_table(object(), "Mesa 7", 4)

    assert result["data"]["display_order"] == 7
    assert conn.fetchrow.await_args.args[5] == 7

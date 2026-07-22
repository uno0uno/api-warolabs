from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.core.exceptions import APIError
from app.services import categories_service


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


def _category_row(category_id, name="Burgers", *, tenant_id=None):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return {
        "id": category_id,
        "name": name,
        "description": None,
        "tenant_id": tenant_id,
        "created_at": now,
        "updated_at": now,
    }


@pytest.mark.asyncio
async def test_reorder_online_menu_categories_rejects_duplicate_ids():
    category_id = uuid4()

    with patch("app.services.categories_service.require_valid_session", return_value=_session()):
        with pytest.raises(APIError) as exc:
            await categories_service.reorder_online_menu_categories(
                object(),
                [category_id, category_id],
            )

    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_reorder_online_menu_categories_rejects_partial_set():
    tenant_id = uuid4()
    first_id = uuid4()
    second_id = uuid4()
    conn = MagicMock()
    conn.transaction.return_value = _tx()
    conn.fetch = AsyncMock(return_value=[{"id": first_id}, {"id": second_id}])

    with (
        patch("app.services.categories_service.require_valid_session", return_value=_session(tenant_id)),
        patch("app.services.categories_service.get_db_connection", side_effect=_db_context(conn)),
    ):
        with pytest.raises(APIError) as exc:
            await categories_service.reorder_online_menu_categories(object(), [first_id])

    assert exc.value.status_code == 400
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_reorder_online_menu_categories_rejects_invisible_category():
    tenant_id = uuid4()
    first_id = uuid4()
    second_id = uuid4()
    conn = MagicMock()
    conn.transaction.return_value = _tx()
    conn.fetch = AsyncMock(side_effect=[
        [{"id": first_id}, {"id": second_id}],
        [{"id": first_id}],
    ])

    with (
        patch("app.services.categories_service.require_valid_session", return_value=_session(tenant_id)),
        patch("app.services.categories_service.get_db_connection", side_effect=_db_context(conn)),
    ):
        with pytest.raises(APIError) as exc:
            await categories_service.reorder_online_menu_categories(
                object(),
                [first_id, second_id],
            )

    assert exc.value.status_code == 404
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_reorder_online_menu_categories_persists_order():
    tenant_id = uuid4()
    first_id = uuid4()
    second_id = uuid4()
    conn = MagicMock()
    conn.transaction.return_value = _tx()
    conn.fetch = AsyncMock(side_effect=[
        [{"id": first_id}, {"id": second_id}],
        [{"id": first_id}, {"id": second_id}],
    ])
    conn.execute = AsyncMock()

    with (
        patch("app.services.categories_service.require_valid_session", return_value=_session(tenant_id)),
        patch("app.services.categories_service.get_db_connection", side_effect=_db_context(conn)),
    ):
        result = await categories_service.reorder_online_menu_categories(
            object(),
            [second_id, first_id],
        )

    assert result["success"] is True
    assert result["data"]["category_ids"] == [str(second_id), str(first_id)]
    assert conn.execute.await_count == 2
    insert_query = conn.execute.await_args_list[1].args[0]
    assert "INSERT INTO tenant_online_menu_category_orders" in insert_query


@pytest.mark.asyncio
async def test_list_online_menu_categories_orders_by_display_order():
    tenant_id = uuid4()
    first_id = uuid4()
    second_id = uuid4()
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[
        _category_row(first_id, "A"),
        _category_row(second_id, "B"),
    ])

    with (
        patch("app.services.categories_service.require_valid_session", return_value=_session(tenant_id)),
        patch("app.services.categories_service.get_db_connection", side_effect=_db_context(conn)),
    ):
        result = await categories_service.list_online_menu_categories(object())

    query = conn.fetch.await_args.args[0]
    assert "tenant_online_menu_category_orders" in query
    assert "o.display_order NULLS LAST" in query
    assert [item.id for item in result["data"]] == [first_id, second_id]


def test_public_menu_sql_uses_saved_category_order():
    categories_sql = categories_service.online_menu_categories_select_sql()
    products_order = categories_service.online_menu_products_order_by_sql()

    assert "tenant_online_menu_category_orders" in categories_sql
    assert "is_available_online = true" in categories_sql
    assert "o.display_order NULLS LAST" in products_order

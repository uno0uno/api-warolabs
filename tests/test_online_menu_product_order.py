from contextlib import asynccontextmanager
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


@pytest.mark.asyncio
async def test_reorder_online_menu_products_rejects_duplicate_ids():
    product_id = uuid4()

    with patch("app.services.categories_service.require_valid_session", return_value=_session()):
        with pytest.raises(APIError) as exc:
            await categories_service.reorder_online_menu_products(
                object(),
                uuid4(),
                [product_id, product_id],
            )

    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_reorder_online_menu_products_rejects_partial_set():
    tenant_id = uuid4()
    category_id = uuid4()
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
            await categories_service.reorder_online_menu_products(
                object(),
                category_id,
                [first_id],
            )

    assert exc.value.status_code == 400
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_reorder_online_menu_products_rejects_wrong_category_membership():
    tenant_id = uuid4()
    category_id = uuid4()
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
            await categories_service.reorder_online_menu_products(
                object(),
                category_id,
                [first_id, second_id],
            )

    assert exc.value.status_code == 404
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_reorder_online_menu_products_persists_order():
    tenant_id = uuid4()
    category_id = uuid4()
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
        result = await categories_service.reorder_online_menu_products(
            object(),
            category_id,
            [second_id, first_id],
        )

    assert result["success"] is True
    assert result["data"]["category_id"] == str(category_id)
    assert result["data"]["product_ids"] == [str(second_id), str(first_id)]
    assert conn.execute.await_count == 2
    delete_query = conn.execute.await_args_list[0].args[0]
    insert_query = conn.execute.await_args_list[1].args[0]
    assert "DELETE FROM tenant_online_menu_product_orders" in delete_query
    assert "INSERT INTO tenant_online_menu_product_orders" in insert_query


@pytest.mark.asyncio
async def test_list_online_menu_products_orders_by_display_order():
    tenant_id = uuid4()
    category_id = uuid4()
    first_id = uuid4()
    second_id = uuid4()
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[
        {
            "id": first_id,
            "name": "A",
            "category_id": category_id,
            "is_available_online": True,
            "is_available_table_qr": False,
            "display_order": 1,
        },
        {
            "id": second_id,
            "name": "B",
            "category_id": category_id,
            "is_available_online": False,
            "is_available_table_qr": True,
            "display_order": 2,
        },
    ])

    with (
        patch("app.services.categories_service.require_valid_session", return_value=_session(tenant_id)),
        patch("app.services.categories_service.get_db_connection", side_effect=_db_context(conn)),
    ):
        result = await categories_service.list_online_menu_products(object(), category_id)

    query = conn.fetch.await_args.args[0]
    assert "tenant_online_menu_product_orders" in query
    assert "po.display_order NULLS LAST" in query
    assert [item["id"] for item in result["data"]] == [first_id, second_id]


def test_product_order_sql_helpers():
    products_order = categories_service.online_menu_products_order_by_sql()
    product_join = categories_service.online_menu_product_order_join_sql()
    qr_categories = categories_service.table_qr_categories_select_sql()

    assert "po.display_order NULLS LAST" in products_order
    assert "tenant_online_menu_product_orders" in product_join
    assert "is_available_table_qr = true" in qr_categories
    assert "tenant_online_menu_category_orders" in qr_categories

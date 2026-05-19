"""Tests for product delete vs archive (warocol.com#705)."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import Request

from app.core.middleware import SessionContext
from app.services.products_service import delete_product


def _session():
    return SessionContext({
        "user_id": uuid4(),
        "tenant_id": uuid4(),
        "email": "test@warocol.com",
        "name": "Test",
        "expires_at": None,
        "is_active": True,
    })


def _mock_conn(*, has_sales: bool, is_available: bool = True, is_available_online: bool = True):
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={
        "id": uuid4(),
        "name": "Test Product",
        "is_available": is_available,
        "is_available_online": is_available_online,
    })
    conn.fetchval = AsyncMock(return_value=has_sales)
    conn.execute = AsyncMock()
    conn.transaction = MagicMock()

    @asynccontextmanager
    async def _txn():
        yield

    conn.transaction.return_value = _txn()
    return conn


@pytest.mark.asyncio
async def test_delete_archives_when_product_has_sales():
    product_id = uuid4()
    request = MagicMock(spec=Request)
    conn = _mock_conn(has_sales=True)

    @asynccontextmanager
    async def _db_ctx():
        yield conn

    with patch("app.services.products_service.require_valid_session", return_value=_session()), \
         patch("app.services.products_service.get_db_connection", side_effect=_db_ctx), \
         patch(
             "app.services.products_service.menu_history_service.get_product_snapshot",
             new=AsyncMock(return_value={"name": "Test Product"}),
         ), \
         patch(
             "app.services.products_service.menu_history_service.record_product_update",
             new=AsyncMock(),
         ) as record_update:
        result = await delete_product(request, product_id)

    assert result["success"] is True
    assert result["archived"] is True
    assert "archived" in result["message"].lower()
    conn.execute.assert_called_once()
    assert "UPDATE product" in conn.execute.call_args[0][0]
    assert record_update.await_count == 2


@pytest.mark.asyncio
async def test_delete_hard_deletes_when_no_sales():
    product_id = uuid4()
    request = MagicMock(spec=Request)
    conn = _mock_conn(has_sales=False)

    @asynccontextmanager
    async def _db_ctx():
        yield conn

    with patch("app.services.products_service.require_valid_session", return_value=_session()), \
         patch("app.services.products_service.get_db_connection", side_effect=_db_ctx), \
         patch(
             "app.services.products_service.menu_history_service.get_product_snapshot",
             new=AsyncMock(return_value={"name": "Test Product"}),
         ), \
         patch(
             "app.services.products_service.menu_history_service.record_product_delete",
             new=AsyncMock(),
         ), \
         patch(
             "app.services.products_service.menu_history_service.record_product_update",
             new=AsyncMock(),
         ) as record_update:
        result = await delete_product(request, product_id)

    assert result["success"] is True
    assert result["archived"] is False
    assert conn.execute.await_count == 2
    delete_calls = [str(c[0][0]) for c in conn.execute.await_args_list]
    assert any("DELETE FROM product_recipes" in q for q in delete_calls)
    assert any("DELETE FROM product" in q for q in delete_calls)
    record_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_idempotent_when_already_archived():
    product_id = uuid4()
    request = MagicMock(spec=Request)
    conn = _mock_conn(has_sales=True, is_available=False, is_available_online=False)

    @asynccontextmanager
    async def _db_ctx():
        yield conn

    with patch("app.services.products_service.require_valid_session", return_value=_session()), \
         patch("app.services.products_service.get_db_connection", side_effect=_db_ctx), \
         patch(
             "app.services.products_service.menu_history_service.get_product_snapshot",
             new=AsyncMock(return_value={}),
         ):
        result = await delete_product(request, product_id)

    assert result["archived"] is True
    conn.execute.assert_not_called()

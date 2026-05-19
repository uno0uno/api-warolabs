"""Tests for duplicate product name → 409 (warocol.com#707)."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import asyncpg
import pytest
from fastapi import HTTPException, Request

from app.core.middleware import SessionContext
from app.models.product import ProductCreate, ProductUpdate
from app.services.products_service import create_product_with_recipe, update_product_with_recipe


def _session():
    return SessionContext({
        "user_id": uuid4(),
        "tenant_id": uuid4(),
        "email": "test@warocol.com",
        "name": "Test",
        "expires_at": None,
        "is_active": True,
    })


def _mock_conn_unique_violation():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=asyncpg.UniqueViolationError("duplicate key"))
    conn.fetchval = AsyncMock(return_value=False)
    conn.execute = AsyncMock()
    conn.transaction = MagicMock()

    @asynccontextmanager
    async def _txn():
        yield

    conn.transaction.return_value = _txn()
    return conn


@pytest.mark.asyncio
async def test_create_product_duplicate_name_returns_409():
    request = MagicMock(spec=Request)
    session = _session()
    conn = _mock_conn_unique_violation()

    @asynccontextmanager
    async def _db_ctx():
        yield conn

    product_data = ProductCreate(
        name="Producción de Hamburguesa de Pollo",
        price=10000,
        category_id=uuid4(),
        tenant_id=session.tenant_id,
    )

    with patch("app.services.products_service.require_valid_session", return_value=session), \
         patch("app.services.products_service.get_db_connection", side_effect=_db_ctx):
        with pytest.raises(HTTPException) as exc:
            await create_product_with_recipe(request, product_data)

    assert exc.value.status_code == 409
    assert "producto" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_update_product_duplicate_name_returns_409():
    request = MagicMock(spec=Request)
    product_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"id": product_id, "name": "Old Name"})
    conn.fetchval = AsyncMock(return_value=False)
    conn.execute = AsyncMock(side_effect=asyncpg.UniqueViolationError("duplicate key"))
    conn.transaction = MagicMock()

    @asynccontextmanager
    async def _txn():
        yield

    conn.transaction.return_value = _txn()

    @asynccontextmanager
    async def _db_ctx():
        yield conn

    product_data = ProductUpdate(name="Producción de Hamburguesa de Pollo")

    with patch("app.services.products_service.require_valid_session", return_value=_session()), \
         patch("app.services.products_service.get_db_connection", side_effect=_db_ctx), \
         patch(
             "app.services.products_service.menu_history_service.get_product_snapshot",
             new=AsyncMock(return_value={"name": "Old Name"}),
         ):
        with pytest.raises(HTTPException) as exc:
            await update_product_with_recipe(request, product_id, product_data)

    assert exc.value.status_code == 409
    assert "producto" in exc.value.detail.lower()

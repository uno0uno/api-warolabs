"""Regression: create_product INSERT must pass 20 bind args (#279, #745)."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import Request

from app.core.middleware import SessionContext
from app.models.product import ProductCreate
from app.services.products_service import create_product_with_recipe


def _session():
    return SessionContext({
        "user_id": uuid4(),
        "tenant_id": uuid4(),
        "email": "test@warocol.com",
        "name": "Test",
        "expires_at": None,
        "is_active": True,
    })


@pytest.mark.asyncio
async def test_create_product_insert_passes_19_bind_args_in_order():
    """INSERT lists is_available_table_qr as $10 — bind must not shift is_combo."""
    request = MagicMock(spec=Request)
    session = _session()
    product_id = uuid4()
    insert_args: list = []

    async def _fetchrow(query, *args):
        if "INSERT INTO product" in query:
            insert_args.extend(args)
            return {
                "id": product_id,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
        return None

    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock(return_value=False)

    @asynccontextmanager
    async def _txn():
        yield

    conn.transaction.return_value = _txn()

    @asynccontextmanager
    async def _db_ctx():
        yield conn

    product_data = ProductCreate(
        name="QR Test Product",
        price=10000,
        category_id=uuid4(),
        tenant_id=session.tenant_id,
        is_available_table_qr=True,
        is_combo=False,
    )

    mock_response = MagicMock()

    with patch("app.services.products_service.require_valid_session", return_value=session), \
         patch("app.services.products_service.get_db_connection", side_effect=_db_ctx), \
         patch("app.services.products_service.menu_history_service.get_product_snapshot", AsyncMock(return_value=None)), \
         patch("app.services.products_service.get_product_by_id", AsyncMock(return_value=mock_response)):
        result = await create_product_with_recipe(request, product_data)

    assert result is mock_response
    assert len(insert_args) == 20
    # $7 controla_stock=True, $8 is_available, $9 is_available_online,
    # $10 is_available_table_qr, $11 is_combo
    assert insert_args[6] is True  # controla_stock
    assert insert_args[9] is True  # is_available_table_qr
    assert insert_args[10] is False  # is_combo (not shifted into $10)

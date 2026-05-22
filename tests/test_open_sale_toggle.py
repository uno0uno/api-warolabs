"""Venta libre toggle + shell product provisioning — warocol.com#805."""
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.core.exceptions import APIError
from app.services import open_priced_service
from app.services.operaciones_context_service import set_open_sale_enabled


@pytest.mark.asyncio
async def test_ensure_open_sale_product_reactivates_existing():
    tenant_id = uuid4()
    product_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {"id": product_id, "name": "Varios"},
        ]
    )
    conn.execute = AsyncMock()

    result = await open_priced_service.ensure_open_sale_product(conn, tenant_id)

    assert result == {"id": str(product_id), "name": "Varios"}
    conn.execute.assert_awaited_once()
    sql = conn.execute.call_args[0][0]
    assert "is_available = true" in sql


@pytest.mark.asyncio
async def test_ensure_open_sale_product_creates_when_missing():
    tenant_id = uuid4()
    category_id = uuid4()
    new_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            None,
            {"id": category_id},
            {"id": new_id, "name": open_priced_service.OPEN_SALE_DEFAULT_NAME},
        ]
    )
    conn.execute = AsyncMock()

    result = await open_priced_service.ensure_open_sale_product(conn, tenant_id)

    assert result["id"] == str(new_id)
    assert result["name"] == "Venta libre"
    assert conn.fetchrow.await_count == 3


@pytest.mark.asyncio
async def test_ensure_open_sale_product_requires_category():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[None, None])

    with pytest.raises(APIError) as excinfo:
        await open_priced_service.ensure_open_sale_product(conn, tenant_id)

    assert excinfo.value.status_code == 409
    assert "categoría" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_set_open_sale_enabled_calls_ensure(monkeypatch):
    tenant_id = uuid4()
    ensure_mock = AsyncMock(return_value={"id": str(uuid4()), "name": "Venta libre"})
    deactivate_mock = AsyncMock()
    fetch_mock = AsyncMock(return_value={"id": str(uuid4()), "name": "Venta libre"})

    monkeypatch.setattr(
        "app.services.operaciones_context_service.ensure_open_sale_product",
        ensure_mock,
    )
    monkeypatch.setattr(
        "app.services.operaciones_context_service.deactivate_open_sale_product",
        deactivate_mock,
    )
    monkeypatch.setattr(
        "app.services.operaciones_context_service.fetch_open_sale_product",
        fetch_mock,
    )

    conn = MagicMock()
    conn.execute = AsyncMock()
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=None)
    txn.__aexit__ = AsyncMock(return_value=None)
    conn.transaction.return_value = txn

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "app.services.operaciones_context_service.get_db_connection",
        lambda: cm,
    )

    result = await set_open_sale_enabled(tenant_id, True)

    ensure_mock.assert_awaited_once()
    deactivate_mock.assert_not_awaited()
    assert result["data"]["open_sale_enabled"] is True
    assert result["data"]["open_sale_product"] is not None


@pytest.mark.asyncio
async def test_set_open_sale_enabled_disable_deactivates(monkeypatch):
    tenant_id = uuid4()
    ensure_mock = AsyncMock()
    deactivate_mock = AsyncMock()
    fetch_mock = AsyncMock(return_value=None)

    monkeypatch.setattr(
        "app.services.operaciones_context_service.ensure_open_sale_product",
        ensure_mock,
    )
    monkeypatch.setattr(
        "app.services.operaciones_context_service.deactivate_open_sale_product",
        deactivate_mock,
    )
    monkeypatch.setattr(
        "app.services.operaciones_context_service.fetch_open_sale_product",
        fetch_mock,
    )

    conn = MagicMock()
    conn.execute = AsyncMock()
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=None)
    txn.__aexit__ = AsyncMock(return_value=None)
    conn.transaction.return_value = txn

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "app.services.operaciones_context_service.get_db_connection",
        lambda: cm,
    )

    result = await set_open_sale_enabled(tenant_id, False)

    deactivate_mock.assert_awaited_once()
    ensure_mock.assert_not_awaited()
    assert result["data"]["open_sale_enabled"] is False
    assert result["data"]["open_sale_product"] is None

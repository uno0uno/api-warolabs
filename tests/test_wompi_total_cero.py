from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.core.exceptions import APIError
from app.services.orders_service import _reject_wompi_zero_total


def test_reject_helper_raises_400():
    with pytest.raises(APIError) as exc:
        _reject_wompi_zero_total()
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_manual_wompi_zero_total_rejected():
    from contextlib import asynccontextmanager
    from datetime import datetime

    from app.services import orders_service

    @asynccontextmanager
    async def _ctx():
        yield conn

    tenant_id = uuid4()
    pid = uuid4()
    conn = MagicMock()
    order_dt = datetime(2026, 9, 10, 10, 0)

    async def _fetch(query, *args):
        if "es_cortesia" in query:
            return [{"id": pid, "es_cortesia": True}]
        return []

    conn.fetch = _fetch

    async def _fetchrow(query, *args):
        return {
            "id": uuid4(),
            "order_number": 3,
            "order_date": order_dt,
            "created_at": order_dt,
        }

    conn.fetchrow = _fetchrow
    conn.execute = AsyncMock()

    @asynccontextmanager
    async def _tx():
        yield None

    conn.transaction.side_effect = lambda: _tx()

    with (
        patch.object(orders_service, "require_valid_session", return_value=SimpleNamespace(user_id=uuid4(), tenant_id=tenant_id)),
        patch.object(orders_service, "get_db_connection", side_effect=lambda: _ctx()),
        patch.object(orders_service, "resolve_tenant_timezone", new=AsyncMock(return_value="America/Bogota")),
        patch.object(orders_service, "assert_order_not_in_closed_monthly_period", new=AsyncMock()),
        patch.object(orders_service, "resolve_modifier_selections", new=AsyncMock(return_value=[])),
    ):
        with pytest.raises(APIError) as exc:
            await orders_service.create_manual_order(
                object(),
                "2026-09-10T10:00",
                "cash",
                [{"product_id": str(pid), "quantity": 1, "unit_price": 0}],
                wompi_collection=True,
            )

    assert exc.value.status_code == 400
    assert "total 0" in str(exc.value)


@pytest.mark.asyncio
async def test_manual_wompi_nonzero_passes_gate():
    from contextlib import asynccontextmanager
    from datetime import datetime

    from app.services import orders_service

    @asynccontextmanager
    async def _ctx():
        yield conn

    tenant_id = uuid4()
    pid = uuid4()
    conn = MagicMock()
    order_dt = datetime(2026, 9, 10, 10, 0)

    async def _fetch(query, *args):
        return []

    conn.fetch = _fetch

    async def _fetchrow(query, *args):
        if "INSERT INTO orders" in query:
            raise AssertionError("passed wompi gate")
        return {
            "id": uuid4(),
            "order_number": 3,
            "order_date": order_dt,
            "created_at": order_dt,
        }

    conn.fetchrow = _fetchrow
    conn.execute = AsyncMock()

    @asynccontextmanager
    async def _tx():
        yield None

    conn.transaction.side_effect = lambda: _tx()

    with (
        patch.object(orders_service, "require_valid_session", return_value=SimpleNamespace(user_id=uuid4(), tenant_id=tenant_id)),
        patch.object(orders_service, "get_db_connection", side_effect=lambda: _ctx()),
        patch.object(orders_service, "resolve_tenant_timezone", new=AsyncMock(return_value="America/Bogota")),
        patch.object(orders_service, "assert_order_not_in_closed_monthly_period", new=AsyncMock()),
        patch.object(orders_service, "resolve_modifier_selections", new=AsyncMock(return_value=[])),
    ):
        from app.core.exceptions import APIError as ServiceAPIError

        with pytest.raises(ServiceAPIError) as exc:
            await orders_service.create_manual_order(
                object(),
                "2026-09-10T10:00",
                "cash",
                [{"product_id": str(pid), "quantity": 1, "unit_price": 100}],
                wompi_collection=True,
            )

    assert "passed wompi gate" in str(exc.value)

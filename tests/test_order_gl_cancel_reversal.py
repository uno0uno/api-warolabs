"""Void + reverse posted sale journals when a completed order is cancelled."""
from datetime import date
from inspect import getsource
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.core.exceptions import APIError
from app.services import cierre_service, orders_service


class _AsyncContext:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _posted_entry(source_module, entry_id=None):
    return {
        "id": entry_id or uuid4(),
        "entry_date": date(2026, 8, 15),
        "period_year": 2026,
        "period_month": 8,
        "description": f"Venta #18401 ({source_module})",
        "total_debit": 45000,
        "total_credit": 45000,
        "source_module": source_module,
    }


@pytest.mark.asyncio
async def test_void_order_gl_entries_voids_orden_and_cogs_with_swapped_lines():
    tenant_id = uuid4()
    order_id = uuid4()
    orden_id = uuid4()
    cogs_id = uuid4()
    rev_orden_id = uuid4()
    rev_cogs_id = uuid4()
    account_a = uuid4()
    account_b = uuid4()

    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_AsyncContext())
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(side_effect=[
        [_posted_entry("orden", orden_id), _posted_entry("orden_cogs", cogs_id)],
        [{"account_id": account_a, "debit": 45000, "credit": 0, "description": "Dr caja", "line_order": 1},
         {"account_id": account_b, "debit": 0, "credit": 45000, "description": "Cr ingreso", "line_order": 2}],
        [{"account_id": account_a, "debit": 20000, "credit": 0, "description": "Dr cogs", "line_order": 1},
         {"account_id": account_b, "debit": 0, "credit": 20000, "description": "Cr inventario", "line_order": 2}],
    ])
    conn.fetchrow = AsyncMock(side_effect=[{"id": rev_orden_id}, {"id": rev_cogs_id}])
    conn.execute = AsyncMock()

    await cierre_service._void_order_gl_entries(
        conn, tenant_id, order_id, reason="Cancelación venta #18401"
    )

    void_updates = [
        call.args[0] for call in conn.execute.await_args_list
        if "status = 'voided'" in call.args[0]
    ]
    assert len(void_updates) == 2
    reversal_insert = conn.fetchrow.await_args_list[0]
    assert "Reversión:" in reversal_insert.args[5]
    assert "Cancelación venta #18401" in reversal_insert.args[5]
    assert "'system'" in reversal_insert.args[0]
    swapped = [
        call.args for call in conn.execute.await_args_list
        if "INSERT INTO tenant_journal_lines" in call.args[0]
    ]
    assert swapped[0][2] == account_a
    assert swapped[0][3] == 0.0
    assert swapped[0][4] == 45000.0
    assert swapped[1][2] == account_b
    assert swapped[1][3] == 45000.0
    assert swapped[1][4] == 0.0
    assert swapped[2][2] == account_a
    assert swapped[2][3] == 0.0
    assert swapped[2][4] == 20000.0
    assert swapped[3][2] == account_b
    assert swapped[3][3] == 20000.0
    assert swapped[3][4] == 0.0
    conn.transaction.assert_called_once()


@pytest.mark.asyncio
async def test_void_order_gl_entries_skips_when_no_posted_rows():
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()
    await cierre_service._void_order_gl_entries(conn, uuid4(), uuid4())
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_post_order_gl_idempotency_ignores_voided_rows():
    src = getsource(cierre_service._post_order_gl_entry)
    cogs_src = getsource(cierre_service._post_order_cogs_gl_entry)
    assert "AND status = 'posted'" in src
    assert "AND status = 'posted'" in cogs_src


@pytest.mark.asyncio
async def test_update_order_status_cancel_voids_gl_not_on_pending():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    void = AsyncMock()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={
        "id": order_id,
        "status": "completed",
        "order_number": 18401,
        "table_session_id": None,
        "pos_cart_id": None,
        "payment_status": "paid",
        "order_date": date(2026, 8, 15),
        "total_amount": 45000,
        "customer_id": None,
    })
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()

    with patch("app.services.orders_service.require_valid_session", return_value=SimpleNamespace(tenant_id=tenant_id, user_id=user_id)), \
         patch("app.services.orders_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.orders_service.resolve_tenant_timezone", new=AsyncMock(return_value="America/Bogota")), \
         patch("app.services.orders_service.assert_order_not_in_closed_monthly_period", new=AsyncMock()), \
         patch("app.services.orders_service._return_stock_for_order_cancellation", new=AsyncMock()), \
         patch("app.services.orders_service._void_order_gl_entries", new=void), \
         patch("app.services.orders_service.record_operation_event", new=AsyncMock()):
        from fastapi import Request
        await orders_service.update_order_status(
            Request({"type": "http"}),
            order_id,
            "cancelled",
            reason="Cancelación de prueba",
        )

    void.assert_awaited_once()
    assert void.await_args.kwargs["reason"] == "Cancelación venta #18401"


@pytest.mark.asyncio
async def test_update_order_status_pending_does_not_void_gl():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    void = AsyncMock()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={
        "id": order_id,
        "status": "completed",
        "order_number": 18401,
        "table_session_id": None,
        "pos_cart_id": None,
        "payment_status": "paid",
        "order_date": date(2026, 8, 15),
        "total_amount": 45000,
        "customer_id": None,
    })
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()

    with patch("app.services.orders_service.require_valid_session", return_value=SimpleNamespace(tenant_id=tenant_id, user_id=user_id)), \
         patch("app.services.orders_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.orders_service.resolve_tenant_timezone", new=AsyncMock(return_value="America/Bogota")), \
         patch("app.services.orders_service.assert_order_not_in_closed_monthly_period", new=AsyncMock()), \
         patch("app.services.orders_service._return_stock_for_order_cancellation", new=AsyncMock()), \
         patch("app.services.orders_service._void_order_gl_entries", new=void), \
         patch("app.services.orders_service.record_operation_event", new=AsyncMock()):
        from fastapi import Request
        with pytest.raises(APIError) as exc:
            await orders_service.update_order_status(Request({"type": "http"}), order_id, "pending")

    assert exc.value.status_code == 400
    void.assert_not_called()


@pytest.mark.asyncio
async def test_update_order_status_cancel_gl_void_failure_raises():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    void = AsyncMock(side_effect=RuntimeError("journal insert failed"))
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={
        "id": order_id,
        "status": "completed",
        "order_number": 18401,
        "table_session_id": None,
        "pos_cart_id": None,
        "payment_status": "paid",
        "order_date": date(2026, 8, 15),
        "total_amount": 45000,
        "customer_id": None,
    })
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()

    with patch("app.services.orders_service.require_valid_session", return_value=SimpleNamespace(tenant_id=tenant_id, user_id=user_id)), \
         patch("app.services.orders_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.orders_service.resolve_tenant_timezone", new=AsyncMock(return_value="America/Bogota")), \
         patch("app.services.orders_service.assert_order_not_in_closed_monthly_period", new=AsyncMock()), \
         patch("app.services.orders_service._return_stock_for_order_cancellation", new=AsyncMock()), \
         patch("app.services.orders_service._void_order_gl_entries", new=void), \
         patch("app.services.orders_service.record_operation_event", new=AsyncMock()):
        from fastapi import Request
        with pytest.raises(APIError) as exc:
            await orders_service.update_order_status(
                Request({"type": "http"}),
                order_id,
                "cancelled",
                reason="Cancelación de prueba",
            )

    assert exc.value.status_code == 500
    assert exc.value.details["code"] == "sale_gl_void_failed"
    void.assert_awaited_once()


@pytest.mark.asyncio
async def test_bulk_update_order_status_cancel_voids_gl():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    void = AsyncMock()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[{
        "id": order_id,
        "status": "completed",
        "order_number": 18401,
        "table_session_id": None,
        "pos_cart_id": None,
        "payment_status": "paid",
        "total_amount": 45000,
    }])
    conn.execute = AsyncMock(return_value="UPDATE 1")

    with patch("app.services.orders_service.require_valid_session", return_value=SimpleNamespace(tenant_id=tenant_id, user_id=user_id)), \
         patch("app.services.orders_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.orders_service.resolve_tenant_timezone", new=AsyncMock(return_value="America/Bogota")), \
         patch("app.services.orders_service._return_stock_for_order_cancellation", new=AsyncMock()), \
         patch("app.services.orders_service._void_order_gl_entries", new=void):
        from fastapi import Request
        await orders_service.bulk_update_order_status(
            Request({"type": "http"}),
            [str(order_id)],
            "cancelled",
        )

    void.assert_awaited_once()
    assert void.await_args.kwargs["reason"] == "Cancelación venta #18401"

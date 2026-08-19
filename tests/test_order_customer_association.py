"""Tests for associating a sale customer before electronic invoicing (#1582)."""
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from app.core.exceptions import APIError
from app.services import orders_service


_TENANT_ID = UUID("93b3e582-34fa-44a6-8d0f-bf82a3608727")
_ORDER_ID = uuid4()
_CUSTOMER_ID = uuid4()


class _ConnCtx:
    def __init__(self, fetchrow_responses):
        self._frows = iter(fetchrow_responses)
        self.conn = MagicMock()
        self.conn.fetchrow = AsyncMock(side_effect=lambda *a, **k: next(self._frows))
        self.conn.execute = AsyncMock()

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_):
        return False


def _patch_session():
    fake = MagicMock()
    fake.tenant_id = _TENANT_ID
    return patch(
        "app.services.orders_service.require_valid_session",
        return_value=fake,
    )


def _patch_db(ctx):
    return patch(
        "app.services.orders_service.get_db_connection",
        lambda *args, **kwargs: ctx,
    )


def _patch_event(new=None):
    return patch(
        "app.services.orders_service.record_operation_event",
        new=new or AsyncMock(),
    )


@pytest.mark.asyncio
async def test_associate_order_customer_updates_when_no_invoice_exists():
    request = MagicMock()
    ctx = _ConnCtx([
        {"id": _ORDER_ID, "customer_id": None, "order_number": 18401},
        None,
        {"id": _CUSTOMER_ID},
    ])

    with _patch_session(), _patch_db(ctx), _patch_event():
        result = await orders_service.associate_order_customer(
            request,
            _ORDER_ID,
            _CUSTOMER_ID,
        )

    assert result == {
        "success": True,
        "message": "Cliente asociado a la venta",
        "customer_id": str(_CUSTOMER_ID),
    }
    ctx.conn.execute.assert_awaited_once()
    assert ctx.conn.execute.await_args.args[1:] == (_CUSTOMER_ID, _ORDER_ID, _TENANT_ID)


@pytest.mark.asyncio
async def test_associate_order_customer_blocks_when_invoice_exists():
    request = MagicMock()
    ctx = _ConnCtx([
        {"id": _ORDER_ID},
        {"id": uuid4()},
    ])

    with _patch_session(), _patch_db(ctx):
        with pytest.raises(APIError) as exc_info:
            await orders_service.associate_order_customer(
                request,
                _ORDER_ID,
                _CUSTOMER_ID,
            )

    assert exc_info.value.status_code == 409
    assert exc_info.value.details == {"code": "invoice_exists"}
    ctx.conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_associate_order_customer_requires_tenant_customer():
    request = MagicMock()
    ctx = _ConnCtx([
        {"id": _ORDER_ID},
        None,
        None,
    ])

    with _patch_session(), _patch_db(ctx), _patch_event():
        with pytest.raises(APIError) as exc_info:
            await orders_service.associate_order_customer(
                request,
                _ORDER_ID,
                _CUSTOMER_ID,
            )

    assert exc_info.value.status_code == 404
    assert "Customer not found" in str(exc_info.value)
    ctx.conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_associate_order_customer_records_ventas_event():
    request = MagicMock()
    user_id = uuid4()
    previous_customer = uuid4()
    recorded = []

    async def capture_record(conn, tid, **kwargs):
        recorded.append({"tenant_id": tid, **kwargs})

    ctx = _ConnCtx([
        {"id": _ORDER_ID, "customer_id": previous_customer, "order_number": 18401},
        None,
        {"id": _CUSTOMER_ID},
    ])
    session = MagicMock()
    session.tenant_id = _TENANT_ID
    session.user_id = user_id

    with patch("app.services.orders_service.require_valid_session", return_value=session), \
         _patch_db(ctx), \
         patch("app.services.orders_service.record_operation_event", new=capture_record):
        await orders_service.associate_order_customer(request, _ORDER_ID, _CUSTOMER_ID)

    assert len(recorded) == 1
    event = recorded[0]
    assert event["domain"] == "ventas"
    assert event["channel"] is None
    assert event["action"] == "order_customer_changed"
    assert event["order_id"] == _ORDER_ID
    assert event["actor_user_id"] == user_id
    assert event["payload"]["old_customer_id"] == str(previous_customer)
    assert event["payload"]["new_customer_id"] == str(_CUSTOMER_ID)
    assert event["payload"]["order_number"] == 18401

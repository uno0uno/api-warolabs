"""POS cart operation audit events (warocol.com#784)."""
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.services import pos_cart_service


@pytest.mark.asyncio
async def test_remove_item_records_cart_line_removed_before_delete():
    tenant_id = uuid4()
    user_id = uuid4()
    cart_id = uuid4()
    item_id = uuid4()
    product_id = uuid4()
    recorded = []
    deleted = []

    row = {
        "id": item_id,
        "product_id": product_id,
        "quantity": 1,
        "unit_price": 12.0,
        "subtotal": 12.0,
        "notes": None,
        "product_name": "Café",
    }

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=row)
    mock_conn.fetch = AsyncMock(return_value=[])

    async def track_execute(sql, *args):
        if "DELETE FROM pos_cart_items" in sql:
            deleted.append(True)

    mock_conn.execute = AsyncMock(side_effect=track_execute)

    class _Txn:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *args):
            return None

    mock_conn.transaction = MagicMock(return_value=_Txn())

    async def capture(conn, tid, **kwargs):
        recorded.append(kwargs)

    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_cm.__aexit__ = AsyncMock(return_value=None)

    with patch("app.services.pos_cart_service.require_valid_session") as mock_sess, \
         patch("app.services.pos_cart_service.get_db_connection", return_value=mock_cm), \
         patch("app.services.pos_cart_service.update_cart_total", new=AsyncMock()), \
         patch(
             "app.services.pos_cart_service._record_cart_operation_event",
             side_effect=capture,
         ):
        mock_sess.return_value = MagicMock(tenant_id=tenant_id, user_id=user_id)
        await pos_cart_service.remove_item_from_cart(
            MagicMock(),
            cart_id,
            item_id,
            channel="barra",
        )

    assert len(recorded) == 1
    assert recorded[0]["action"] == "cart_line_removed"
    assert recorded[0]["channel"] == "barra"
    assert recorded[0]["payload"]["product_name"] == "Café"
    assert len(deleted) == 1


@pytest.mark.asyncio
async def test_clear_cart_emits_cart_cleared_per_line():
    tenant_id = uuid4()
    user_id = uuid4()
    cart_id = uuid4()
    line_id = uuid4()
    recorded = []

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value={"id": cart_id})
    mock_conn.fetch = AsyncMock(side_effect=[
        [
            {
                "cart_item_id": line_id,
                "product_id": uuid4(),
                "quantity": 2,
                "unit_price": 5.0,
                "subtotal": 10.0,
                "notes": None,
                "product_name": "Agua",
            },
        ],
        [],
    ])
    mock_conn.execute = AsyncMock()

    class _Txn:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *args):
            return None

    mock_conn.transaction = MagicMock(return_value=_Txn())

    async def capture(conn, tid, **kwargs):
        recorded.append(kwargs)

    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_cm.__aexit__ = AsyncMock(return_value=None)

    with patch("app.services.pos_cart_service.require_valid_session") as mock_sess, \
         patch("app.services.pos_cart_service.get_db_connection", return_value=mock_cm), \
         patch(
             "app.services.pos_cart_service._record_cart_operation_event",
             side_effect=capture,
         ):
        mock_sess.return_value = MagicMock(tenant_id=tenant_id, user_id=user_id)
        await pos_cart_service.clear_cart(
            MagicMock(),
            cart_id,
            channel="mostrador",
        )

    assert len(recorded) == 1
    assert recorded[0]["action"] == "cart_cleared"
    assert recorded[0]["channel"] == "mostrador"

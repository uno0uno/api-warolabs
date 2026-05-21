"""Payment void reason persistence and operation events (warocol.com#785)."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.services import pos_cart_service, tables_service


def _txn_conn():
    mock_conn = AsyncMock()

    class _Txn:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *args):
            return None

    mock_conn.transaction = MagicMock(return_value=_Txn())
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_cm.__aexit__ = AsyncMock(return_value=None)
    return mock_conn, mock_cm


@pytest.mark.asyncio
async def test_void_order_payment_persists_reason_and_records_event():
    tenant_id = uuid4()
    user_id = uuid4()
    cart_id = uuid4()
    payment_id = uuid4()
    order_id = uuid4()
    recorded = []
    void_updates = []

    payment_row = {
        "id": payment_id,
        "order_id": order_id,
        "amount": 50.0,
        "payment_method": "cash",
        "cash_received": 60.0,
        "created_by_user_id": user_id,
        "voided_at": None,
        "pos_cart_id": cart_id,
        "total_amount": 100.0,
        "tip_amount": 0,
        "tip_tax_amount": 0,
        "payment_status": "partial",
        "order_status": "active",
    }

    mock_conn, mock_cm = _txn_conn()
    mock_conn.fetchrow = AsyncMock(side_effect=[
        payment_row,
        {"paid": 0},
    ])

    async def track_execute(sql, *args):
        if "void_reason" in sql:
            void_updates.append(args)

    mock_conn.execute = AsyncMock(side_effect=track_execute)

    async def capture_event(conn, tid, **kwargs):
        recorded.append(kwargs)

    with patch("app.services.pos_cart_service.require_valid_session") as mock_sess, \
         patch("app.services.pos_cart_service.get_db_connection", return_value=mock_cm), \
         patch(
             "app.services.pos_cart_service.record_operation_event",
             side_effect=capture_event,
         ):
        mock_sess.return_value = MagicMock(tenant_id=tenant_id, user_id=user_id, role="admin")
        await pos_cart_service.void_order_payment(
            MagicMock(),
            str(cart_id),
            str(payment_id),
            reason="Cliente se arrepintió",
            channel="barra",
        )

    assert len(void_updates) == 1
    assert void_updates[0][1] == "Cliente se arrepintió"
    assert len(recorded) == 1
    assert recorded[0]["action"] == "payment_voided"
    assert recorded[0]["channel"] == "barra"
    assert recorded[0]["reason"] == "Cliente se arrepintió"
    assert recorded[0]["actor_user_id"] == user_id
    assert recorded[0]["payload"]["amount"] == 50.0


@pytest.mark.asyncio
async def test_void_order_payment_empty_reason_defaults_sin_motivo():
    tenant_id = uuid4()
    user_id = uuid4()
    cart_id = uuid4()
    payment_id = uuid4()
    order_id = uuid4()
    void_updates = []

    payment_row = {
        "id": payment_id,
        "order_id": order_id,
        "amount": 10.0,
        "payment_method": "card",
        "cash_received": None,
        "created_by_user_id": user_id,
        "voided_at": None,
        "pos_cart_id": cart_id,
        "total_amount": 10.0,
        "tip_amount": 0,
        "tip_tax_amount": 0,
        "payment_status": "partial",
        "order_status": "active",
    }

    mock_conn, mock_cm = _txn_conn()
    mock_conn.fetchrow = AsyncMock(side_effect=[payment_row, {"paid": 0}])

    async def track_execute(sql, *args):
        if "void_reason" in sql:
            void_updates.append(args)

    mock_conn.execute = AsyncMock(side_effect=track_execute)

    with patch("app.services.pos_cart_service.require_valid_session") as mock_sess, \
         patch("app.services.pos_cart_service.get_db_connection", return_value=mock_cm), \
         patch("app.services.pos_cart_service.record_operation_event", new=AsyncMock()):
        mock_sess.return_value = MagicMock(tenant_id=tenant_id, user_id=user_id, role="admin")
        await pos_cart_service.void_order_payment(
            MagicMock(), str(cart_id), str(payment_id), reason="  ",
        )

    assert void_updates[0][1] == "Sin motivo"


@pytest.mark.asyncio
async def test_void_table_payment_records_single_payment_voided_event():
    tenant_id = uuid4()
    user_id = uuid4()
    table_id = uuid4()
    payment_id = uuid4()
    session_id = uuid4()
    order_id = uuid4()
    paid_at = datetime.now(timezone.utc)
    recorded = []
    void_updates = []

    target = {
        "id": payment_id,
        "order_id": order_id,
        "payment_method": "cash",
        "paid_at": paid_at,
        "cash_received": 100.0,
        "created_by_user_id": user_id,
        "voided_at": None,
        "table_session_id": session_id,
    }
    session_row = {"id": session_id, "table_id": table_id, "closed_at": None}
    sibling_rows = [
        {"id": payment_id, "order_id": order_id, "amount": 40.0},
        {"id": uuid4(), "order_id": uuid4(), "amount": 10.0},
    ]

    mock_conn, mock_cm = _txn_conn()
    mock_conn.fetchrow = AsyncMock(side_effect=[
        target,
        session_row,
        {"is_bar": True},
        {"paid": 0},
        {"tip_amount": 0, "tip_tax_amount": 0},
    ])
    mock_conn.fetch = AsyncMock(side_effect=[
        sibling_rows,
        [{"id": order_id, "total_amount": 50.0}],
    ])

    async def track_execute(sql, *args):
        if "void_reason" in sql:
            void_updates.append(args)

    mock_conn.execute = AsyncMock(side_effect=track_execute)

    async def capture_event(conn, tid, **kwargs):
        recorded.append(kwargs)

    with patch("app.services.tables_service.require_valid_session") as mock_sess, \
         patch("app.services.tables_service.get_db_connection", return_value=mock_cm), \
         patch(
             "app.services.tables_service.record_operation_event",
             side_effect=capture_event,
         ):
        mock_sess.return_value = MagicMock(tenant_id=tenant_id, user_id=user_id, role="admin")
        await tables_service.void_table_payment(
            MagicMock(), table_id, payment_id, reason="Error de caja",
        )

    assert void_updates[0][1] == "Error de caja"
    assert len(recorded) == 1
    assert recorded[0]["action"] == "payment_voided"
    assert recorded[0]["channel"] == "barra"
    assert recorded[0]["reason"] == "Error de caja"
    assert len(recorded[0]["payload"]["voided_ids"]) == 2
    assert recorded[0]["payload"]["amount"] == 50.0

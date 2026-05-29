"""close_session release path — cancel pending orders on liberar mesa (#330)."""
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.core.exceptions import APIError
from app.services import tables_service


def _mock_conn(*, session_id, pending_count=1, pending_line_count=1, cancelled_count=1):
    executed: list[str] = []
    fetchval_calls: list[str] = []
    mock_conn = AsyncMock()

    async def track_execute(sql, *args):
        executed.append(sql.strip())

    async def track_fetchval(sql, *args):
        fetchval_calls.append(sql.strip())
        if "COUNT(*) FROM orders WHERE table_session_id" in sql and "pending" in sql:
            if len([c for c in fetchval_calls if "COUNT(*) FROM orders" in c]) == 1:
                return pending_count
            return cancelled_count
        if "COUNT(*) FROM order_items" in sql:
            return pending_line_count
        return cancelled_count

    mock_conn.execute = AsyncMock(side_effect=track_execute)
    mock_conn.fetchrow = AsyncMock(
        side_effect=[
            {"id": uuid4(), "is_bar": False, "name": "Mesa 1"},
            {"id": session_id, "effective_waiter_member_id": None},
        ]
    )
    mock_conn.fetchval = AsyncMock(side_effect=track_fetchval)
    mock_conn.fetch = AsyncMock(return_value=[])

    class _Txn:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *args):
            return None

    mock_conn.transaction = MagicMock(return_value=_Txn())
    return mock_conn, executed, fetchval_calls


def _connection_cm(mock_conn):
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_cm.__aexit__ = AsyncMock(return_value=None)
    return mock_cm


@pytest.mark.asyncio
async def test_cancel_pending_session_orders_on_release_updates_comandas_and_orders():
    tenant_id = uuid4()
    session_id = uuid4()
    mock_conn, executed, fetchval_calls = _mock_conn(session_id=session_id)

    cancelled = await tables_service._cancel_pending_session_orders_on_release(
        mock_conn,
        tenant_id,
        session_id,
    )

    assert cancelled == 1
    assert any("UPDATE comanda_items" in s and "cancelled" in s for s in executed)
    assert any("UPDATE comandas" in s and "cancelled" in s for s in executed)
    assert any("UPDATE orders" in s and "cancelled" in s for s in fetchval_calls)


@pytest.mark.asyncio
async def test_close_session_release_cancels_pending_orders():
    tenant_id = uuid4()
    user_id = uuid4()
    table_id = uuid4()
    session_id = uuid4()

    mock_conn, _, _ = _mock_conn(session_id=session_id, pending_count=2, pending_line_count=2)
    mock_cm = _connection_cm(mock_conn)

    with patch("app.services.tables_service.require_valid_session") as mock_sess, \
         patch("app.services.tables_service.get_db_connection", return_value=mock_cm), \
         patch(
             "app.services.tables_service._record_tab_cleared_pending_lines",
             new=AsyncMock(return_value=2),
         ), \
         patch(
             "app.services.tables_service._cancel_pending_session_orders_on_release",
             new=AsyncMock(return_value=2),
         ) as mock_cancel, \
         patch(
             "app.services.tables_service.fire_comandas",
             new=AsyncMock(),
         ) as mock_fire:
        mock_sess.return_value = MagicMock(tenant_id=tenant_id, user_id=user_id)
        result = await tables_service.close_session(
            MagicMock(),
            table_id,
            payment_method=None,
            reason="Cliente se fue",
        )

    mock_cancel.assert_called_once()
    mock_fire.assert_not_called()
    assert result["data"]["pending_orders"] == 0


@pytest.mark.asyncio
async def test_close_session_release_without_reason_raises_400():
    tenant_id = uuid4()
    user_id = uuid4()
    table_id = uuid4()
    session_id = uuid4()

    mock_conn, _, _ = _mock_conn(session_id=session_id, pending_count=1, pending_line_count=1)
    mock_cm = _connection_cm(mock_conn)

    with patch("app.services.tables_service.require_valid_session") as mock_sess, \
         patch("app.services.tables_service.get_db_connection", return_value=mock_cm):
        mock_sess.return_value = MagicMock(tenant_id=tenant_id, user_id=user_id)
        with pytest.raises(APIError) as exc:
            await tables_service.close_session(
                MagicMock(),
                table_id,
                payment_method=None,
                reason="",
            )
    assert exc.value.status_code == 400

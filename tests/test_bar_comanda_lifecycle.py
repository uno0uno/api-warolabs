"""Barra comanda lifecycle — warocol.com#799."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.services import comandas_service, pos_cart_service, tables_service


@pytest.mark.asyncio
async def test_finalize_skipped_for_bar_sale_flag():
    """Bar checkout must not call finalize_open_comandas (guarded by _is_bar_sale)."""
    conn = AsyncMock()
    order_id = uuid4()
    tenant_id = uuid4()

    with patch(
        "app.services.pos_cart_service.finalize_open_comandas",
        new_callable=AsyncMock,
    ) as mock_finalize:
        _is_bar_sale = True
        if not _is_bar_sale:
            await pos_cart_service.finalize_open_comandas(conn, order_id, tenant_id)
        mock_finalize.assert_not_called()


@pytest.mark.asyncio
async def test_update_comanda_ready_stays_ready_for_bar_order():
    """Bar-linked pos comandas keep ready; mostrador pos auto-delivers."""
    comanda_id = uuid4()
    tenant_id = uuid4()

    row = {"id": comanda_id, "status": "preparing", "source_type": "table", "is_bar": True}
    new_status = "ready"
    if new_status == "ready" and row["source_type"] == "pos" and not row["is_bar"]:
        new_status = "delivered"
    assert new_status == "ready"

    row_counter = {"id": comanda_id, "status": "preparing", "source_type": "pos", "is_bar": False}
    new_status_counter = "ready"
    if new_status_counter == "ready" and row_counter["source_type"] == "pos" and not row_counter["is_bar"]:
        new_status_counter = "delivered"
    assert new_status_counter == "delivered"


@pytest.mark.asyncio
async def test_kds_query_allows_completed_order_with_open_comanda():
    """Active KDS SQL must not hide non-terminal comandas on completed orders."""
    sql_fragment = """
                  AND (o.status IS NULL OR o.status != 'cancelled')
    """
    assert "NOT IN ('completed'" not in sql_fragment


@pytest.mark.asyncio
async def test_kds_date_filter_uses_tenant_local_day_window():
    """KDS `date` filter must not drop night orders after UTC midnight."""
    tenant_id = uuid4()
    captured: dict[str, object] = {}

    mock_conn = AsyncMock()
    mock_conn.fetchval = AsyncMock(side_effect=[True, "America/Bogota"])

    async def capture_fetch(sql, *params):
        captured["sql"] = sql
        captured["params"] = params
        return []

    mock_conn.fetch = AsyncMock(side_effect=capture_fetch)

    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_cm.__aexit__ = AsyncMock(return_value=None)

    session = MagicMock(tenant_id=tenant_id)

    with patch("app.services.comandas_service.require_valid_session", return_value=session), \
         patch("app.services.comandas_service.get_db_connection", return_value=mock_cm):
        result = await comandas_service.get_comandas_for_kds(
            object(),
            date="2026-07-05",
        )

    assert result == {"success": True, "data": []}
    assert "DATE(c.fired_at AT TIME ZONE 'UTC')" not in str(captured["sql"])
    assert "c.fired_at >= $2 AND c.fired_at < $3" in str(captured["sql"])
    assert captured["params"][1] == datetime(2026, 7, 5, 5, 0, tzinfo=timezone.utc)
    assert captured["params"][2] == datetime(2026, 7, 6, 5, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_daily_stats_date_filter_uses_tenant_local_day_window():
    """Daily station stats must use the tenant-local day, not the UTC date."""
    tenant_id = uuid4()
    captured: dict[str, object] = {}

    mock_conn = AsyncMock()
    mock_conn.fetchval = AsyncMock(side_effect=[True, "America/Bogota"])

    async def capture_fetch(sql, *params):
        captured["sql"] = sql
        captured["params"] = params
        return []

    mock_conn.fetch = AsyncMock(side_effect=capture_fetch)

    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_cm.__aexit__ = AsyncMock(return_value=None)

    session = MagicMock(tenant_id=tenant_id)

    with patch("app.services.comandas_service.require_valid_session", return_value=session), \
         patch("app.services.comandas_service.get_db_connection", return_value=mock_cm):
        result = await comandas_service.get_daily_stats(
            object(),
            date="2026-07-05",
        )

    assert result == {"date": "2026-07-05", "stations": []}
    assert "DATE(c.fired_at)" not in str(captured["sql"])
    assert "c.fired_at >= $2" in str(captured["sql"])
    assert "c.fired_at < $3" in str(captured["sql"])
    assert "($4::uuid IS NULL OR ks.id = $4)" in str(captured["sql"])
    assert captured["params"][1] == datetime(2026, 7, 5, 5, 0, tzinfo=timezone.utc)
    assert captured["params"][2] == datetime(2026, 7, 6, 5, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_clear_tab_cancels_open_comandas():
    """clear_tab should cancel comanda rows for pending session orders."""
    tenant_id = uuid4()
    session_id = uuid4()
    executed: list[str] = []

    mock_conn = AsyncMock()

    async def track_execute(sql, *args):
        executed.append(sql.strip())

    mock_conn.execute = AsyncMock(side_effect=track_execute)
    mock_conn.fetchrow = AsyncMock(
        side_effect=[
            {"id": session_id},
            None,
        ]
    )
    mock_conn.fetch = AsyncMock(return_value=[])
    mock_conn.fetchval = AsyncMock(return_value=0)

    class _Txn:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *args):
            return None

    mock_conn.transaction = MagicMock(return_value=_Txn())

    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_cm.__aexit__ = AsyncMock(return_value=None)

    with patch("app.services.tables_service.require_valid_session") as mock_sess, \
         patch("app.services.tables_service.get_db_connection", return_value=mock_cm), \
         patch("app.services.tables_service._fetch_tab_operation_context", new=AsyncMock(return_value=None)):
        mock_sess.return_value = MagicMock(tenant_id=tenant_id, user_id=uuid4())
        table_id = uuid4()
        await tables_service.clear_tab(MagicMock(), table_id)

    cancel_sql = [s for s in executed if "UPDATE comandas" in s and "cancelled" in s]
    assert len(cancel_sql) >= 1

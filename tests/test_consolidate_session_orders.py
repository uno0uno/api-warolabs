"""Mesa checkout consolidation — one order per session close (#686)."""
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services import tables_service


def _sql_contains(args, needle: str) -> bool:
    return needle in str(args[0])


@pytest.fixture
def stub_merge(monkeypatch):
    merge_mock = AsyncMock()
    recalc_mock = AsyncMock()
    monkeypatch.setattr(tables_service, "_merge_order_into_primary", merge_mock)
    monkeypatch.setattr(tables_service, "_recalc_order_total_from_items", recalc_mock)
    return merge_mock


@pytest.mark.asyncio
async def test_merge_order_repoints_comandas_and_items():
    primary_id = uuid4()
    secondary_id = uuid4()
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=[])
    mock_conn.execute = AsyncMock()

    await tables_service._merge_order_into_primary(mock_conn, primary_id, secondary_id)

    execute_sql = [str(c.args[0]) for c in mock_conn.execute.await_args_list]
    assert any("UPDATE order_items SET order_id" in s for s in execute_sql)
    assert any("UPDATE order_payments SET order_id" in s for s in execute_sql)
    assert any("UPDATE comandas SET order_id" in s for s in execute_sql)
    assert any("DELETE FROM orders" in s for s in execute_sql)


@pytest.mark.asyncio
async def test_merge_order_sums_overlapping_variant_lines():
    primary_id = uuid4()
    secondary_id = uuid4()
    primary_item_id = uuid4()
    secondary_item_id = uuid4()
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(
        return_value=[
            {
                "secondary_item_id": secondary_item_id,
                "primary_item_id": primary_item_id,
                "sec_qty": 2,
                "sec_subtotal": 20.0,
                "sec_net": 18.0,
                "sec_discount": 2.0,
            }
        ]
    )
    mock_conn.execute = AsyncMock()

    await tables_service._merge_order_into_primary(mock_conn, primary_id, secondary_id)

    update_calls = [
        c for c in mock_conn.execute.await_args_list
        if _sql_contains(c.args, "quantity = quantity +")
    ]
    assert len(update_calls) == 1
    assert update_calls[0].args[1:] == (primary_item_id, 2, 20.0, 18.0, 2.0)
    comanda_item_updates = [
        c for c in mock_conn.execute.await_args_list
        if _sql_contains(c.args, "UPDATE comanda_items SET order_item_id")
    ]
    assert len(comanda_item_updates) == 1
    delete_item_calls = [
        c for c in mock_conn.execute.await_args_list
        if _sql_contains(c.args, "DELETE FROM order_items WHERE id")
    ]
    assert len(delete_item_calls) == 1


@pytest.mark.asyncio
async def test_consolidate_merges_multiple_pending_into_oldest(stub_merge):
    session_id = uuid4()
    primary_id = uuid4()
    secondary_id = uuid4()
    fetch_results = [
        [{"id": primary_id}, {"id": secondary_id}],
        [{"id": primary_id}],
        [],
        [],
    ]
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(side_effect=fetch_results)

    await tables_service._consolidate_session_orders_for_checkout(mock_conn, session_id)

    stub_merge.assert_awaited_once_with(mock_conn, primary_id, secondary_id)


@pytest.mark.asyncio
async def test_consolidate_folds_pending_into_completed_partial(stub_merge):
    session_id = uuid4()
    completed_id = uuid4()
    pending_id = uuid4()
    fetch_results = [
        [{"id": pending_id}],
        [{"id": pending_id}],
        [{"id": completed_id}],
        [{"id": completed_id}],
    ]
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(side_effect=fetch_results)

    await tables_service._consolidate_session_orders_for_checkout(mock_conn, session_id)

    stub_merge.assert_awaited_once_with(mock_conn, completed_id, pending_id)


@pytest.mark.asyncio
async def test_consolidate_skips_pending_fold_when_disabled(stub_merge):
    session_id = uuid4()
    completed_id = uuid4()
    pending_id = uuid4()
    sibling_id = uuid4()
    fetch_results = [
        [{"id": pending_id}],
        [{"id": completed_id}, {"id": sibling_id}],
    ]
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(side_effect=fetch_results)

    await tables_service._consolidate_session_orders_for_checkout(
        mock_conn,
        session_id,
        fold_pending_into_completed=False,
    )

    stub_merge.assert_awaited_once_with(mock_conn, completed_id, sibling_id)


@pytest.mark.asyncio
async def test_consolidate_merges_multiple_completed_split_orders(stub_merge):
    session_id = uuid4()
    primary_id = uuid4()
    sibling_id = uuid4()
    fetch_results = [
        [],
        [{"id": primary_id}, {"id": sibling_id}],
    ]
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(side_effect=fetch_results)

    await tables_service._consolidate_session_orders_for_checkout(
        mock_conn,
        session_id,
        fold_pending_into_completed=False,
    )

    stub_merge.assert_awaited_once_with(mock_conn, primary_id, sibling_id)


@pytest.mark.asyncio
async def test_consolidate_noop_for_single_pending_order(stub_merge):
    session_id = uuid4()
    order_id = uuid4()
    fetch_results = [
        [{"id": order_id}],
        [{"id": order_id}],
        [],
        [],
    ]
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(side_effect=fetch_results)

    await tables_service._consolidate_session_orders_for_checkout(mock_conn, session_id)

    stub_merge.assert_not_awaited()

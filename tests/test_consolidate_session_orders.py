"""Mesa checkout consolidation — one order per session close (#686)."""
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services import tables_service


@pytest.mark.asyncio
async def test_consolidate_merges_multiple_pending_into_oldest():
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
    mock_conn.execute = AsyncMock()

    await tables_service._consolidate_session_orders_for_checkout(mock_conn, session_id)

    assert mock_conn.fetch.call_count == 4
    merge_calls = [
        c for c in mock_conn.execute.await_args_list
        if "UPDATE order_items SET order_id" in str(c.args[0])
    ]
    assert len(merge_calls) == 1
    assert merge_calls[0].args[1:] == (primary_id, secondary_id)
    delete_calls = [c for c in mock_conn.execute.await_args_list if "DELETE FROM orders" in str(c.args[0])]
    assert len(delete_calls) == 1
    recalc_calls = [c for c in mock_conn.execute.await_args_list if "SET total_amount = COALESCE" in str(c.args[0])]
    assert len(recalc_calls) == 1
    assert recalc_calls[0].args[1] == primary_id


@pytest.mark.asyncio
async def test_consolidate_folds_pending_into_completed_partial():
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
    mock_conn.execute = AsyncMock()

    await tables_service._consolidate_session_orders_for_checkout(mock_conn, session_id)

    merge_calls = [
        c for c in mock_conn.execute.await_args_list
        if "UPDATE order_items SET order_id" in str(c.args[0])
    ]
    assert len(merge_calls) == 1
    assert merge_calls[0].args[1:] == (completed_id, pending_id)


@pytest.mark.asyncio
async def test_consolidate_merges_multiple_completed_split_orders():
    session_id = uuid4()
    primary_id = uuid4()
    sibling_id = uuid4()
    fetch_results = [
        [],
        [],
        [{"id": primary_id}, {"id": sibling_id}],
        [{"id": primary_id}, {"id": sibling_id}],
    ]
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(side_effect=fetch_results)
    mock_conn.execute = AsyncMock()

    await tables_service._consolidate_session_orders_for_checkout(mock_conn, session_id)

    merge_calls = [
        c for c in mock_conn.execute.await_args_list
        if "UPDATE order_items SET order_id" in str(c.args[0])
    ]
    assert len(merge_calls) == 1
    assert merge_calls[0].args[1:] == (primary_id, sibling_id)


@pytest.mark.asyncio
async def test_consolidate_noop_for_single_pending_order():
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
    mock_conn.execute = AsyncMock()

    await tables_service._consolidate_session_orders_for_checkout(mock_conn, session_id)

    mock_conn.execute.assert_not_called()

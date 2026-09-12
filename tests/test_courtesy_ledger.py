"""Courtesy ledger: orden_cortesia source + audit (uno0uno/warocol.com#2671)."""
from contextlib import asynccontextmanager
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest


def _conn_with(fetchval_map, insert_capture):
    conn = MagicMock()

    async def _fetchval(query, *args):
        if "tenant_journal_entries" in query:
            return fetchval_map.get("existing")
        if "SUM(oii.total_cost)" in query:
            return 1200.0
        if "FROM order_items oi" in query:
            return fetchval_map.get("non_courtesy", 1)
        return None

    async def _fetchrow(query, *args):
        if "INSERT INTO tenant_journal_entries" in query:
            insert_capture["query"] = query
            insert_capture["args"] = args
            return {"id": uuid4()}
        return {}

    conn.fetchval = _fetchval
    conn.fetchrow = _fetchrow
    conn.execute = AsyncMock()

    @asynccontextmanager
    async def _tx():
        yield None

    conn.transaction.side_effect = lambda: _tx()
    return conn


def _run_cogs(fetchval_map):
    from app.services import cierre_service

    insert_capture: dict = {}
    conn = _conn_with(fetchval_map, insert_capture)
    tenant_id, order_id = uuid4(), uuid4()

    async def _go():
        with patch.object(
            cierre_service, "resolve_account",
            new=AsyncMock(return_value=SimpleNamespace(id=uuid4())),
        ):
            await cierre_service._post_order_cogs_gl_entry(
                conn, tenant_id, order_id, date(2026, 9, 12), order_number=9
            )

    return insert_capture, order_id, _go


@pytest.mark.asyncio
async def test_cogs_all_courtesy_uses_orden_cortesia():
    insert_capture, _, _go = _run_cogs({"non_courtesy": 0})
    await _go()
    assert insert_capture["args"][5] == "orden_cortesia"
    assert "cortesía" in insert_capture["args"][4]


@pytest.mark.asyncio
async def test_cogs_mixed_uses_orden_cogs():
    insert_capture, _, _go = _run_cogs({"non_courtesy": 2})
    await _go()
    assert insert_capture["args"][5] == "orden_cogs"


@pytest.mark.asyncio
async def test_cogs_idempotent_across_both_modules():
    from app.services import cierre_service

    insert_capture: dict = {}
    queries: list = []
    orig_fetchval = None

    conn = MagicMock()

    async def _fetchval(query, *args):
        queries.append(query)
        if "tenant_journal_entries" in query:
            return uuid4()
        return None

    conn.fetchval = _fetchval
    with patch.object(
        cierre_service, "resolve_account",
        new=AsyncMock(return_value=SimpleNamespace(id=uuid4())),
    ):
        await cierre_service._post_order_cogs_gl_entry(
            conn, uuid4(), uuid4(), date(2026, 9, 12), order_number=9
        )
    assert insert_capture == {}
    assert any(
        "orden_cortesia" in q and "tenant_journal_entries" in q for q in queries
    )


@pytest.mark.asyncio
async def test_void_reverses_courtesy_entry():
    from app.services import cierre_service

    entry_id = uuid4()
    conn = MagicMock()
    calls = {"n": 0}
    seen_queries: list = []

    async def _fetch(query, *args):
        calls["n"] += 1
        seen_queries.append(query)
        if calls["n"] == 1:
            return [{
                "id": entry_id, "entry_date": date(2026, 9, 12),
                "period_year": 2026, "period_month": 9,
                "description": "CMV #9 — cortesía",
                "total_debit": 1200.0, "total_credit": 1200.0,
                "source_module": "orden_cortesia",
            }]
        return [{
            "account_id": uuid4(), "debit": 1200.0, "credit": 0.0,
            "description": "CMV #9 — cortesía", "line_order": 0,
        }]

    conn.fetch = _fetch
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(return_value={"id": uuid4()})
    conn.execute = AsyncMock()

    @asynccontextmanager
    async def _tx():
        yield None

    conn.transaction.side_effect = lambda: _tx()

    await cierre_service._void_order_gl_entries(conn, uuid4(), uuid4(), reason="test")

    posted = [q for q in seen_queries if "tenant_journal_entries" in q]
    assert any("orden_cortesia" in q for q in posted)
    voids = [c.args[0] for c in conn.execute.await_args_list if "voided" in c.args[0]]
    assert voids, "expected void UPDATE"


@pytest.mark.asyncio
async def test_manual_order_audits_courtesy():
    import json
    from contextlib import asynccontextmanager

    from app.services import orders_service

    tenant_id, pid = uuid4(), uuid4()
    conn = MagicMock()
    order_id = uuid4()

    async def _fetch(query, *args):
        if "es_cortesia" in query:
            return [{"id": pid, "es_cortesia": True}]
        if "FROM product_recipes" in query:
            return []
        return []

    async def _fetchrow(query, *args):
        if "INSERT INTO orders" in query:
            _fetchrow.captured = args
            return {"id": order_id, "order_number": 3,
                    "order_date": args[4], "created_at": args[4]}
        if "INSERT INTO order_items" in query:
            return {"id": uuid4()}
        if "SELECT current_stock" in query:
            return {"current_stock": 10}
        if "INSERT INTO order_payments" in query:
            return {"id": uuid4()}
        return {}

    conn.fetch = _fetch
    conn.fetchrow = _fetchrow
    conn.execute = AsyncMock()

    @asynccontextmanager
    async def _tx():
        yield None

    conn.transaction.side_effect = lambda: _tx()

    @asynccontextmanager
    async def _ctx():
        yield conn

    with (
        patch.object(orders_service, "require_valid_session", return_value=SimpleNamespace(user_id=uuid4(), tenant_id=tenant_id)),
        patch.object(orders_service, "get_db_connection", side_effect=lambda: _ctx()),
        patch.object(orders_service, "resolve_tenant_timezone", new=AsyncMock(return_value="America/Bogota")),
        patch.object(orders_service, "assert_order_not_in_closed_monthly_period", new=AsyncMock()),
        patch.object(orders_service, "resolve_modifier_selections", new=AsyncMock(return_value=[])),
        patch.object(orders_service, "_pos_modifier_inventory_helpers", return_value=(AsyncMock(), None, None)),
        patch.object(orders_service, "_pos_order_item_ingredient_snapshot_helper", return_value=AsyncMock()),
        patch.object(orders_service, "_get_tenant_tax_config", new=AsyncMock(return_value={})),
        patch.object(orders_service, "_post_order_gl_entry", new=AsyncMock()),
        patch.object(orders_service, "_post_order_cogs_gl_entry", new=AsyncMock()),
        patch("app.services.credit_service.sync_order_split_credit_status", new=AsyncMock(return_value="settled")),
    ):
        result = await orders_service.create_manual_order(
            object(), "2026-09-10T10:00", "cash",
            [{"product_id": str(pid), "quantity": 1, "unit_price": 0}],
            courtesy_reason="Cliente frecuente",
        )

    assert result["success"] is True
    extra = json.loads(_fetchrow.captured[10])
    assert extra["source"] == "manual"
    assert extra["courtesy"]["reason"] == "Cliente frecuente"
    assert extra["courtesy"]["by"] is not None

"""Courtesy analytics exclusion (uno0uno/warocol.com#2670)."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest


def _ctx(conn):
    @asynccontextmanager
    async def _inner():
        yield conn

    return _inner()


@pytest.mark.asyncio
async def test_menu_analysis_excludes_courtesy_and_reports_summary():
    from app.services import analytics_service

    conn = MagicMock()
    queries = []

    async def _fetch(query, *args):
        queries.append(query)
        return []

    async def _fetchrow(query, *args):
        queries.append(query)
        return {"courtesy_units": 7, "courtesy_orders": 5}

    conn.fetch = _fetch
    conn.fetchrow = _fetchrow

    with (
        patch.object(analytics_service, "get_db_connection", side_effect=lambda: _ctx(conn)),
        patch.object(analytics_service, "resolve_tenant_timezone", new=AsyncMock(return_value="America/Bogota")),
    ):
        result = await analytics_service._get_menu_analysis_for_tenant(
            str(uuid4()), date_from="2026-01-01", date_to="2026-09-12"
        )

    assert result["success"] is True
    assert result["data"]["menu_items"] == []
    assert result["data"]["summary"]["courtesy_units"] == 7
    assert result["data"]["summary"]["courtesy_orders"] == 5
    assert any("es_cortesia" in q for q in queries)


@pytest.mark.asyncio
async def test_metrics_avg_excludes_zero_orders_and_reports_courtesy():
    from app.services import orders_service

    tenant_id = uuid4()
    conn = MagicMock()
    queries = []

    async def _fetchrow(query, *args):
        queries.append(query)
        return {
            "total_sales": 100.0,
            "total_orders": 3,
            "completed_orders": 3,
            "cancelled_orders": 0,
            "pending_orders": 0,
            "avg_ticket": 50.0,
            "discount_count": 0,
            "total_discount_amount": 0.0,
            "courtesy_units": 4,
            "courtesy_orders": 2,
        }

    conn.fetchrow = _fetchrow
    conn.fetch = AsyncMock(return_value=[])

    with (
        patch.object(orders_service, "require_valid_session", return_value=SimpleNamespace(user_id=uuid4(), tenant_id=tenant_id)),
        patch.object(orders_service, "get_db_connection", side_effect=lambda: _ctx(conn)),
        patch.object(orders_service, "resolve_tenant_timezone", new=AsyncMock(return_value="America/Bogota")),
        patch.object(orders_service, "_get_tenant_tax_config", new=AsyncMock(return_value={})),
        patch.object(orders_service, "_compute_tax_breakdown", return_value=(0.0, 0.0, "Impuesto")),
    ):
        result = await orders_service.get_orders_metrics(
            object(), date_from="2026-09-01", date_to="2026-09-12"
        )

    assert result["success"] is True
    assert result["data"]["courtesy_units"] == 4
    assert result["data"]["courtesy_orders"] == 2
    assert any("total_amount > 0" in q for q in queries)
    # Rama sin categoria tambien trae columnas cortesia (vista por defecto).
    assert any("courtesy_units" in q and "es_cortesia" in q for q in queries)

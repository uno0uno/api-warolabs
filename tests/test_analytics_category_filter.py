"""Analytics category filter (#1926) — optional category_id scopes item-attributed revenue."""
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import analytics_service, orders_service

CATEGORY_UUID = "11111111-1111-1111-1111-111111111111"
TENANT_UUID = "00000000-0000-0000-0000-000000000099"


def _mock_conn(fetch_impl=None):
    mock_conn = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value="America/Mexico_City")
    if fetch_impl:
        mock_conn.fetch = fetch_impl
    else:
        mock_conn.fetch = AsyncMock(return_value=[])
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_cm.__aexit__ = AsyncMock(return_value=None)
    return mock_cm, mock_conn


@pytest.mark.asyncio
async def test_menu_analysis_with_category_id_filters_sql_and_args():
    captured_query = []
    captured_args = []

    async def capture_fetch(query, *args):
        captured_query.append(query)
        captured_args.append(args)
        return []

    mock_cm, _ = _mock_conn(capture_fetch)

    with patch("app.services.analytics_service.get_db_connection", return_value=mock_cm):
        await analytics_service._get_menu_analysis_for_tenant(
            TENANT_UUID,
            "2026-01-01",
            "2026-01-31",
            limit=10,
            category_id=CATEGORY_UUID,
        )

    sql = captured_query[0]
    assert "p.category_id = $6::uuid" in sql
    assert CATEGORY_UUID in captured_args[0]


@pytest.mark.asyncio
async def test_menu_analysis_without_category_id_omits_category_filter():
    captured_query = []
    captured_args = []

    async def capture_fetch(query, *args):
        captured_query.append(query)
        captured_args.append(args)
        return []

    mock_cm, _ = _mock_conn(capture_fetch)

    with patch("app.services.analytics_service.get_db_connection", return_value=mock_cm):
        await analytics_service._get_menu_analysis_for_tenant(
            TENANT_UUID,
            "2026-01-01",
            "2026-01-31",
            limit=10,
        )

    sql = captured_query[0]
    assert "p.category_id = $" not in sql
    assert CATEGORY_UUID not in captured_args[0]
    assert captured_args[0][4] == 10


@pytest.mark.asyncio
async def test_food_cost_with_category_id_filters_sql_and_args():
    captured_query = []
    captured_args = []

    async def capture_fetch(query, *args):
        captured_query.append(query)
        captured_args.append(args)
        return []

    mock_cm, _ = _mock_conn(capture_fetch)

    with patch("app.services.analytics_service.get_db_connection", return_value=mock_cm):
        await analytics_service._get_food_cost_for_tenant(
            TENANT_UUID,
            "2026-01-01",
            "2026-01-31",
            category_id=CATEGORY_UUID,
        )

    sql = captured_query[0]
    assert "JOIN product p ON p.id = oi.product_id" in sql
    assert "p.category_id = $7::uuid" in sql
    assert CATEGORY_UUID in captured_args[0]


def test_get_orders_metrics_accepts_category_id():
    sig = inspect.signature(orders_service.get_orders_metrics)
    assert "category_id" in sig.parameters

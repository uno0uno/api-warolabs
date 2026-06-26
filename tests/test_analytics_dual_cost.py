"""Analytics dual cost (#747) — menu-analysis and food-cost use stored product costs."""
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import analytics_service


def test_product_costs_cte_uses_stored_columns():
    assert "p.costo_calculado" in analytics_service._PRODUCT_COSTS_CTE
    assert "p.costo_percibido" in analytics_service._PRODUCT_COSTS_CTE
    assert "cost_used_for_classification" in analytics_service._PRODUCT_COSTS_CTE
    assert "p.price * 0.40" in analytics_service._PRODUCT_COSTS_CTE


@pytest.mark.asyncio
async def test_menu_analysis_response_includes_dual_margin_fields():
    fake_row = {
        "id": "00000000-0000-0000-0000-000000000001",
        "name": "Test Burger",
        "category_name": "Platos",
        "price": Decimal("20000"),
        "estimated_cost": Decimal("8000"),
        "costo_percibido": Decimal("6000"),
        "effective_cost": Decimal("6000"),
        "cost_used_for_classification": "operativo",
        "order_count": 5,
        "total_units_sold": 10,
        "total_revenue": Decimal("200000"),
        "avg_price": Decimal("20000"),
        "profit_per_unit": Decimal("14000"),
        "profit_margin_pct": Decimal("70.0"),
        "profit_margin_real_pct": Decimal("150.0"),
        "profit_margin_operativo_pct": Decimal("233.3"),
        "total_profit": Decimal("140000"),
        "classification": "Star",
    }

    mock_conn = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value="America/Mexico_City")
    mock_conn.fetch = AsyncMock(return_value=[fake_row])
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_cm.__aexit__ = AsyncMock(return_value=None)

    with patch("app.services.analytics_service.get_db_connection", return_value=mock_cm):
        result = await analytics_service._get_menu_analysis_for_tenant(
            "00000000-0000-0000-0000-000000000099",
            "2026-01-01",
            "2026-01-31",
            limit=10,
        )

    item = result["data"]["menu_items"][0]
    assert item["estimated_cost"] == 8000.0
    assert item["costo_percibido"] == 6000.0
    assert item["cost_used_for_classification"] == "operativo"
    assert item["profit_margin_real_pct"] == 150.0
    assert item["profit_margin_operativo_pct"] == 233.3


@pytest.mark.asyncio
async def test_menu_analysis_classification_uses_operativo_margin():
    """BCG query uses effective_cost; operativo product has higher margin % on price."""
    captured_query = []
    captured_args = []

    async def capture_fetch(query, *args):
        captured_query.append(query)
        captured_args.append(args)
        return []

    mock_conn = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value="America/Mexico_City")
    mock_conn.fetch = capture_fetch
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_cm.__aexit__ = AsyncMock(return_value=None)

    with patch("app.services.analytics_service.get_db_connection", return_value=mock_cm):
        await analytics_service._get_menu_analysis_for_tenant(
            "00000000-0000-0000-0000-000000000099",
            "2026-01-01",
            "2026-01-31",
        )

    sql = captured_query[0]
    assert "effective_cost" in sql
    assert "cost_used_for_classification" in sql
    assert "profit_margin_operativo_pct" in sql
    assert "latest_ingredient_costs" not in sql
    assert "AT TIME ZONE $4" in sql
    assert "AT TIME ZONE 'America/Bogota'" not in sql
    assert captured_args[0][3] == "America/Mexico_City"


@pytest.mark.asyncio
async def test_food_cost_includes_operativo_pct():
    rows = [
        {
            "period": "current",
            "revenue": Decimal("100000"),
            "total_cost": Decimal("35000"),
            "food_cost_pct": Decimal("35"),
            "food_cost_operativo_pct": Decimal("30"),
        },
        {
            "period": "previous",
            "revenue": Decimal("80000"),
            "total_cost": Decimal("28000"),
            "food_cost_pct": Decimal("35"),
            "food_cost_operativo_pct": Decimal("32"),
        },
    ]

    mock_conn = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value="America/Mexico_City")
    mock_conn.fetch = AsyncMock(return_value=rows)
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_cm.__aexit__ = AsyncMock(return_value=None)

    with patch("app.services.analytics_service.get_db_connection", return_value=mock_cm):
        result = await analytics_service._get_food_cost_for_tenant(
            "00000000-0000-0000-0000-000000000099",
            "2026-01-01",
            "2026-01-31",
        )

    cur = result["data"]["current_period"]
    assert cur["food_cost_pct"] == 35.0
    assert cur["food_cost_operativo_pct"] == 30.0

"""Ingredient analytics API contracts (#1565)."""
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.services import analytics_service


def _mock_db(fetch_rows=None, fetchrow_rows=None):
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value="America/Mexico_City")
    conn.fetch = AsyncMock(side_effect=list(fetch_rows or []))
    conn.fetchrow = AsyncMock(side_effect=list(fetchrow_rows or []))

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    return conn, cm


@pytest.mark.asyncio
async def test_ingredient_summary_uses_recorded_consumption_and_purchase_costs():
    ingredient_id = uuid4()
    rows = [{
        "ingredient_id": ingredient_id,
        "ingredient_name": "Tomate",
        "category": "Verduras",
        "unit": "gr",
        "consumed_quantity": Decimal("1234.5"),
        "purchase_quantity": Decimal("2000"),
        "latest_cost_per_unit": Decimal("6.6171"),
        "latest_cost_at": datetime(2026, 1, 20, 12, 0),
        "weighted_avg_cost_per_unit": Decimal("6.42"),
        "estimated_consumed_cost": Decimal("7925.49"),
        "movement_count": 12,
        "data_coverage": "recorded_movements",
    }]
    conn, cm = _mock_db(fetch_rows=[rows])

    with patch("app.services.analytics_service.get_db_connection", return_value=cm):
        result = await analytics_service._get_ingredient_analytics_summary_for_tenant(
            "tenant-1",
            date_from="2026-01-01",
            date_to="2026-01-31",
            ingredient_id=ingredient_id,
            category="Verduras",
            unit="gr",
            quantity_min=100,
            quantity_max=2000,
            limit=25,
            sort="estimated_consumed_cost_desc",
        )

    sql = conn.fetch.await_args.args[0]
    args = conn.fetch.await_args.args
    assert "tenant_ingredient_movements tim" in sql
    assert "tim.movement_type = 'consumption'" in sql
    assert "tim.quantity_change < 0" in sql
    assert "tenant_purchase_items tpi" in sql
    assert "SUM(tpi.quantity * tpi.unit_cost) / NULLIF(SUM(tpi.quantity), 0)" in sql
    assert "p.costo_calculado" not in sql
    assert "p.costo_percibido" not in sql
    assert "p.price * 0.40" not in sql
    assert "ORDER BY estimated_consumed_cost DESC NULLS LAST" in sql
    assert args[4] == "America/Mexico_City"
    assert args[5] == ingredient_id
    assert args[6] == "Verduras"
    assert args[7] == "gr"
    assert args[8] == 100
    assert args[9] == 2000
    assert args[10] == 25

    item = result["data"]["items"][0]
    assert item["ingredient_id"] == str(ingredient_id)
    assert item["consumed_quantity"] == 1234.5
    assert item["purchase_quantity"] == 2000.0
    assert item["latest_cost_per_unit"] == 6.6171
    assert item["weighted_avg_cost_per_unit"] == 6.42
    assert item["estimated_consumed_cost"] == 7925.49
    assert item["movement_count"] == 12
    assert item["data_coverage"] == "recorded_movements"


@pytest.mark.asyncio
async def test_ingredient_summary_defaults_period_and_whitelists_sort():
    conn, cm = _mock_db(fetch_rows=[[]])

    with patch("app.services.analytics_service.get_db_connection", return_value=cm), \
         patch("app.services.analytics_service.tenant_today", return_value=datetime(2026, 7, 9).date()):
        await analytics_service._get_ingredient_analytics_summary_for_tenant(
            "tenant-1",
            sort="unsafe desc; drop table product",
        )

    sql = conn.fetch.await_args.args[0]
    args = conn.fetch.await_args.args
    assert "unsafe desc" not in sql
    assert "ORDER BY consumed_quantity DESC NULLS LAST" in sql
    assert args[2].isoformat() == "2026-01-01"
    assert args[3].isoformat() == "2026-07-09"


@pytest.mark.asyncio
async def test_ingredient_history_returns_purchase_consumption_stock_and_products():
    ingredient_id = uuid4()
    purchase_id = uuid4()
    purchase_item_id = uuid4()
    movement_id = uuid4()
    product_id = uuid4()
    rows = [
        [{
            "purchase_item_id": purchase_item_id,
            "purchase_id": purchase_id,
            "purchase_number": "CP-1",
            "is_direct_entry": True,
            "purchase_date": datetime(2026, 1, 5, 10, 0),
            "base_quantity": Decimal("1345"),
            "base_unit": "gr",
            "purchase_quantity": Decimal("1.345"),
            "purchase_unit": "kg",
            "unit_cost": Decimal("6.6171"),
            "total_cost": Decimal("8900"),
            "received_at": datetime(2026, 1, 5, 10, 15),
        }],
        [{
            "id": movement_id,
            "movement_type": "consumption",
            "quantity_change": Decimal("-250"),
            "unit": "gr",
            "previous_stock": Decimal("1000"),
            "new_stock": Decimal("750"),
            "cost_per_unit": Decimal("6.6171"),
            "reference_table": "orders",
            "reference_id": uuid4(),
            "reference_order_number": 16208,
            "reason": "POS sale",
            "notes": None,
            "created_at": datetime(2026, 1, 6, 12, 0),
        }],
        [{
            "product_id": product_id,
            "product_name": "Hamburguesa",
            "relation_type": "direct_recipe",
            "quantity": Decimal("50"),
            "unit": "gr",
        }],
    ]
    conn, cm = _mock_db(
        fetch_rows=rows,
        fetchrow_rows=[
            {"id": ingredient_id, "name": "Tomate", "category": "Verduras", "unit": "gr"},
            {
                "current_stock": Decimal("750"),
                "minimum_stock": Decimal("100"),
                "maximum_stock": Decimal("2000"),
                "last_updated": datetime(2026, 1, 6, 12, 5),
                "location": "Bodega",
            },
        ],
    )

    with patch("app.services.analytics_service.get_db_connection", return_value=cm):
        result = await analytics_service._get_ingredient_analytics_history_for_tenant(
            "tenant-1",
            ingredient_id,
            date_from="2026-01-01",
            date_to="2026-01-31",
            limit=10,
        )

    purchase_sql = conn.fetch.await_args_list[0].args[0]
    movement_sql = conn.fetch.await_args_list[1].args[0]
    related_sql = conn.fetch.await_args_list[2].args[0]
    assert "tenant_purchase_items tpi" in purchase_sql
    assert "tp.is_direct_entry" in purchase_sql
    assert "tim.movement_type = 'consumption'" in movement_sql
    assert "LEFT JOIN orders o" in movement_sql
    assert "o.order_number AS reference_order_number" in movement_sql
    assert "product_recipes pr" in related_sql
    assert "base_recipe_templates brt" in related_sql

    data = result["data"]
    assert data["ingredient"]["id"] == str(ingredient_id)
    assert data["purchases"][0]["unit_cost"] == 6.6171
    assert data["purchases"][0]["is_direct_entry"] is True
    assert data["consumption_movements"][0]["consumed_quantity"] == 250.0
    assert data["consumption_movements"][0]["reference_order_number"] == 16208
    assert data["stock"]["current_stock"] == 750.0
    assert data["related_products"][0]["product_id"] == str(product_id)
    assert data["data_coverage"] == "recorded_movements"


@pytest.mark.asyncio
async def test_ingredient_report_returns_metrics_series_and_history():
    ingredient_id = uuid4()
    purchase_id = uuid4()
    purchase_item_id = uuid4()
    movement_id = uuid4()
    product_id = uuid4()
    rows = [
        [
            {
                "history_type": "purchase",
                "purchase_item_id": purchase_item_id,
                "purchase_id": purchase_id,
                "purchase_number": "CP-2",
                "is_direct_entry": True,
                "purchase_date": datetime(2026, 1, 5, 10, 0),
                "base_quantity": Decimal("2000"),
                "base_unit": "gr",
                "purchase_quantity": Decimal("2"),
                "purchase_unit": "kg",
                "unit_cost": Decimal("6.5"),
                "total_cost": Decimal("13000"),
                "received_at": datetime(2026, 1, 5, 10, 15),
            },
            {
                "history_type": "movement",
                "movement_id": movement_id,
                "movement_type": "consumption",
                "quantity_change": Decimal("-300"),
                "movement_unit": "gr",
                "previous_stock": Decimal("1000"),
                "new_stock": Decimal("700"),
                "movement_cost_per_unit": Decimal("6.7"),
                "reference_table": "orders",
                "reference_id": uuid4(),
                "reference_order_number": 16208,
                "reason": "POS sale",
                "notes": None,
                "movement_created_at": datetime(2026, 1, 6, 12, 0),
            },
        ],
        [{
            "product_id": product_id,
            "product_name": "Hamburguesa",
            "relation_type": "direct_recipe",
            "quantity": Decimal("50"),
            "unit": "gr",
        }],
        [{
            "period": datetime(2026, 1, 6).date(),
            "consumed_quantity": Decimal("300"),
            "estimated_consumed_cost": Decimal("1950"),
            "unit_cost": Decimal("6.5"),
            "movement_count": 1,
        }],
        [{
            "period": datetime(2026, 1, 1).date(),
            "consumed_quantity": Decimal("300"),
            "estimated_consumed_cost": Decimal("1950"),
            "unit_cost": Decimal("6.5"),
            "movement_count": 1,
        }],
    ]
    conn, cm = _mock_db(
        fetch_rows=rows,
        fetchrow_rows=[
            {"id": ingredient_id, "name": "Tomate", "category": "Verduras", "unit": "gr"},
            {
                "consumed_quantity": Decimal("300"),
                "movement_count": 1,
                "purchase_quantity": Decimal("2000"),
                "weighted_avg_cost_per_unit": Decimal("6.5"),
                "latest_cost_per_unit": Decimal("6.7"),
                "latest_cost_at": datetime(2026, 1, 6, 12, 0),
                "estimated_consumed_cost": Decimal("1950"),
                "cost_basis": "weighted_avg_purchase_cost",
                "first_unit_cost": Decimal("6.0"),
                "latest_purchase_unit_cost": Decimal("6.5"),
                "cost_variation": Decimal("0.5"),
                "cost_variation_pct": Decimal("0.0833333333"),
            },
            {"total_count": 2},
            {
                "current_stock": Decimal("700"),
                "minimum_stock": Decimal("100"),
                "maximum_stock": Decimal("2000"),
                "last_updated": datetime(2026, 1, 6, 12, 5),
                "location": "Bodega",
            },
        ],
    )

    with patch("app.services.analytics_service.get_db_connection", return_value=cm):
        result = await analytics_service._get_ingredient_analytics_report_for_tenant(
            "tenant-1",
            ingredient_id,
            date_from="2026-01-01",
            date_to="2026-01-31",
            limit=10,
            offset=10,
            history_type="consumption",
            record_kind="order",
            unit="gr",
            quantity_min=100,
            quantity_max=500,
        )

    metrics_sql = conn.fetchrow.await_args_list[1].args[0]
    count_sql = conn.fetchrow.await_args_list[2].args[0]
    history_sql = conn.fetch.await_args_list[0].args[0]
    day_sql = conn.fetch.await_args_list[2].args[0]
    month_sql = conn.fetch.await_args_list[3].args[0]
    assert "tim.movement_type = 'consumption'" in metrics_sql
    assert "DATE(tim.created_at AT TIME ZONE $5)" in metrics_sql
    assert "tenant_purchase_items tpi" in count_sql
    assert "WITH history AS" in history_sql
    assert "tp.is_direct_entry" in history_sql
    assert "LEFT JOIN orders o" in history_sql
    assert "o.order_number AS reference_order_number" in history_sql
    assert "$6::text IS NULL" in count_sql
    assert "$7::text IS NULL" in count_sql
    assert "LIMIT $11 OFFSET $12" in history_sql
    assert conn.fetch.await_args_list[0].args[6] == "consumption"
    assert conn.fetch.await_args_list[0].args[7] == "order"
    assert conn.fetch.await_args_list[0].args[8] == "gr"
    assert conn.fetch.await_args_list[0].args[9] == 100
    assert conn.fetch.await_args_list[0].args[10] == 500
    assert conn.fetch.await_args_list[0].args[11] == 10
    assert conn.fetch.await_args_list[0].args[12] == 10
    assert "DATE(tim.created_at AT TIME ZONE $5) AS period" in day_sql
    assert "DATE_TRUNC('month', tim.created_at AT TIME ZONE $5)::date AS period" in month_sql
    assert conn.fetchrow.await_args_list[1].args[5] == "America/Mexico_City"

    data = result["data"]
    assert data["ingredient"]["id"] == str(ingredient_id)
    assert data["period"]["timezone"] == "America/Mexico_City"
    assert data["metrics"]["consumed_quantity"] == 300.0
    assert data["metrics"]["estimated_consumed_cost"] == 1950.0
    assert data["metrics"]["weighted_avg_cost_per_unit"] == 6.5
    assert data["metrics"]["latest_cost_per_unit"] == 6.7
    assert data["metrics"]["cost_basis"] == "weighted_avg_purchase_cost"
    assert data["metrics"]["cost_variation"] == 0.5
    assert data["metrics"]["data_coverage"] == "recorded_movements"
    assert data["series"]["day"][0]["period"] == "2026-01-06"
    assert data["series"]["month"][0]["period"] == "2026-01-01"
    assert data["purchases"][0]["purchase_id"] == str(purchase_id)
    assert data["purchases"][0]["is_direct_entry"] is True
    assert data["consumption_movements"][0]["consumed_quantity"] == 300.0
    assert data["consumption_movements"][0]["reference_order_number"] == 16208
    assert data["stock"]["current_stock"] == 700.0
    assert data["related_products"][0]["product_id"] == str(product_id)
    assert data["history_pagination"] == {
        "limit": 10,
        "offset": 10,
        "total": 2,
        "has_more": False,
        "history_type": "consumption",
        "record_kind": "order",
        "unit": "gr",
        "quantity_min": 100,
        "quantity_max": 500,
    }


@pytest.mark.asyncio
async def test_ingredient_summary_wrapper_resolves_tenant_before_core():
    session = SimpleNamespace(tenant_id="tenant-wrapper")

    with patch("app.services.analytics_service.require_valid_session", return_value=session), \
         patch(
             "app.services.analytics_service._get_ingredient_analytics_summary_for_tenant",
             new_callable=AsyncMock,
         ) as core:
        core.return_value = {"success": True, "data": {"items": []}}
        result = await analytics_service.get_ingredient_analytics_summary(
            object(),
            date_from="2026-01-01",
            date_to="2026-01-31",
            unit="gr",
            quantity_min=10,
            quantity_max=250,
            limit=5,
        )

    assert result["success"] is True
    core.assert_awaited_once_with(
        "tenant-wrapper",
        date_from="2026-01-01",
        date_to="2026-01-31",
        ingredient_id=None,
        category=None,
        unit="gr",
        quantity_min=10,
        quantity_max=250,
        limit=5,
        sort="consumed_quantity_desc",
    )


@pytest.mark.asyncio
async def test_ingredient_report_wrapper_resolves_tenant_before_core():
    session = SimpleNamespace(tenant_id="tenant-wrapper")
    ingredient_id = uuid4()

    with patch("app.services.analytics_service.require_valid_session", return_value=session), \
         patch(
             "app.services.analytics_service._get_ingredient_analytics_report_for_tenant",
             new_callable=AsyncMock,
         ) as core:
        core.return_value = {"success": True, "data": {"ingredient": {"id": str(ingredient_id)}}}
        result = await analytics_service.get_ingredient_analytics_report(
            object(),
            ingredient_id,
            date_from="2026-01-01",
            date_to="2026-01-31",
            limit=5,
            offset=15,
            history_type="purchase",
            record_kind="purchase",
            unit="kg",
            quantity_min=1,
            quantity_max=5,
        )

    assert result["success"] is True
    core.assert_awaited_once_with(
        "tenant-wrapper",
        ingredient_id,
        date_from="2026-01-01",
        date_to="2026-01-31",
        limit=5,
        offset=15,
        history_type="purchase",
        record_kind="purchase",
        unit="kg",
        quantity_min=1,
        quantity_max=5,
    )

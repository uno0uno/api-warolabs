from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi import Request

from app.core.exceptions import ValidationError
from app.services import queries_service


TENANT_ID = "93b3e582-34fa-44a6-8d0f-bf82a3608727"


class _ConnCtx:
    latest_conn = None

    def __init__(self, rows):
        self._rows = rows

    async def __aenter__(self):
        conn = MagicMock()
        conn.fetch = AsyncMock(return_value=self._rows)
        _ConnCtx.latest_conn = conn
        return conn

    async def __aexit__(self, *_):
        return False


def _request():
    return Request({"type": "http"})


def test_queries_schema_lists_mvp_datasets():
    schema = queries_service.get_queries_schema()
    datasets = {item["name"]: item for item in schema["data"]["datasets"]}

    assert set(datasets) == {
        "sales_items",
        "customers",
        "product_profitability",
        "inventory_stock",
        "inventory_movements",
        "purchase_items",
    }
    assert "revenue" in {field["name"] for field in datasets["sales_items"]["measures"]}
    assert "total_spent" in {field["name"] for field in datasets["customers"]["measures"]}
    assert "profit_margin_operativo_pct" in {
        field["name"] for field in datasets["product_profitability"]["measures"]
    }
    assert datasets["inventory_stock"]["required_scope"] == "inventory:read"
    assert datasets["inventory_movements"]["required_scope"] == "inventory:read"
    assert datasets["purchase_items"]["required_scope"] == "purchases:read"
    assert "stock_value" in {field["name"] for field in datasets["inventory_stock"]["measures"]}
    assert "net_quantity" in {field["name"] for field in datasets["inventory_movements"]["measures"]}
    assert "total_cost" in {field["name"] for field in datasets["purchase_items"]["measures"]}
    assert datasets["purchase_items"]["limitations"]


@pytest.mark.parametrize(
    "patch_spec,field",
    [
        ({"dataset": "orders; DROP TABLE orders"}, "dataset"),
        ({"measures": ["SUM(total_amount)"]}, "measures"),
        ({"dimensions": ["product; DROP TABLE product"]}, "dimensions"),
        ({"filters": {"sql": "1=1"}}, "filters"),
        ({"order_by": [{"field": "revenue; DROP", "direction": "desc"}]}, "order_by.0.field"),
        ({"limit": None}, "limit"),
    ],
)
def test_compile_queryspec_rejects_non_allowlisted_payloads(patch_spec, field):
    spec = {
        "dataset": "sales_items",
        "measures": ["revenue"],
        "dimensions": ["product"],
        "filters": {},
        "order_by": [{"field": "revenue", "direction": "desc"}],
        "limit": 10,
    }
    spec.update(patch_spec)

    with pytest.raises(ValidationError) as exc:
        queries_service.compile_queryspec(spec)

    assert field in str(exc.value.details)


def test_compile_queryspec_builds_parameterized_sales_items_sql():
    compiled = queries_service.compile_queryspec(
        {
            "dataset": "sales_items",
            "measures": ["quantity_sold", "revenue"],
            "dimensions": ["product", "category"],
            "filters": {
                "date_range": {"from": "2026-01-01", "to": "2026-01-31"},
                "category": "Bebidas",
            },
            "order_by": [{"field": "revenue", "direction": "desc"}],
            "limit": 20,
        }
    )

    sql = compiled["sql"]
    assert "DROP" not in sql
    assert "Bebidas" not in sql
    assert "p.name AS product" in sql
    assert "ORDER BY revenue DESC NULLS LAST" in sql
    assert "c.name = $6" in sql
    assert "LIMIT $7" in sql
    assert compiled["params"] == [
        date(2026, 1, 1),
        "America/Bogota",
        date(2026, 1, 31),
        "America/Bogota",
        "Bebidas",
        20,
    ]


def test_compile_product_profitability_uses_product_costs_cte():
    compiled = queries_service.compile_queryspec(
        {
            "dataset": "product_profitability",
            "measures": ["revenue", "profit_margin_operativo_pct", "total_profit"],
            "dimensions": ["product", "classification"],
            "filters": {},
            "order_by": [{"field": "total_profit", "direction": "desc"}],
            "limit": 5,
        }
    )

    assert "WITH" in compiled["sql"]
    assert "product_costs AS" in compiled["sql"]
    assert "costo_percibido" in compiled["sql"]
    assert "JOIN product_costs pc" in compiled["sql"]


def test_compile_product_profitability_accepts_cost_source_dimension():
    compiled = queries_service.compile_queryspec(
        {
            "dataset": "product_profitability",
            "measures": ["quantity_sold", "profit_margin_pct", "profit_margin_real_pct", "revenue"],
            "dimensions": ["product", "cost_source"],
            "filters": {},
            "order_by": [{"field": "quantity_sold", "direction": "desc"}],
            "limit": 20,
        }
    )

    assert [column["name"] for column in compiled["columns"]] == [
        "product",
        "cost_source",
        "quantity_sold",
        "profit_margin_pct",
        "profit_margin_real_pct",
        "revenue",
    ]
    assert compiled["columns"][1]["role"] == "dimension"
    assert "pc.cost_used_for_classification AS cost_source" in compiled["sql"]
    assert "pc.cost_used_for_classification" in compiled["sql"]


def test_compile_inventory_stock_queryspec_uses_tenant_scope_and_latest_costs():
    compiled = queries_service.compile_queryspec(
        {
            "dataset": "inventory_stock",
            "measures": ["current_stock", "stock_value"],
            "dimensions": ["ingredient", "category", "status"],
            "filters": {"category": "Bebidas"},
            "order_by": [{"field": "stock_value", "direction": "desc"}],
            "limit": 25,
        }
    )

    sql = compiled["sql"]
    assert "WITH" in sql
    assert "latest_costs AS" in sql
    assert "FROM tenant_inventory ti" in sql
    assert "JOIN ingredients i ON ti.ingredient_id = i.id" in sql
    assert "ti.tenant_id = $1" in sql
    assert "i.category = $2" in sql
    assert "Bebidas" not in sql
    assert "ORDER BY stock_value DESC NULLS LAST" in sql
    assert "LIMIT $3" in sql
    assert compiled["params"] == ["Bebidas", 25]


def test_compile_inventory_movements_queryspec_parameterizes_dates_and_filters():
    compiled = queries_service.compile_queryspec(
        {
            "dataset": "inventory_movements",
            "measures": ["quantity_in", "quantity_out", "net_quantity", "movement_count"],
            "dimensions": ["ingredient", "movement_type", "day"],
            "filters": {
                "date_range": {"from": "2026-02-01", "to": "2026-02-28"},
                "movement_type": "purchase",
            },
            "order_by": [{"field": "net_quantity", "direction": "desc"}],
            "limit": 30,
        }
    )

    sql = compiled["sql"]
    assert "FROM tenant_ingredient_movements tim" in sql
    assert "LEFT JOIN tenant_purchases tp" in sql
    assert "tim.tenant_id = $1" in sql
    assert "tim.created_at >= ($2::timestamp AT TIME ZONE $3)" in sql
    assert "tim.created_at < (($4::timestamp + interval '1 day') AT TIME ZONE $5)" in sql
    assert "tim.movement_type = $6" in sql
    assert "tim.movement_type = 'purchase'" not in sql
    assert "LIMIT $7" in sql
    assert compiled["params"] == [
        date(2026, 2, 1),
        "America/Bogota",
        date(2026, 2, 28),
        "America/Bogota",
        "purchase",
        30,
    ]


def test_compile_purchase_items_queryspec_uses_purchase_scope_and_boolean_filter():
    compiled = queries_service.compile_queryspec(
        {
            "dataset": "purchase_items",
            "measures": ["quantity_purchased", "total_cost", "avg_unit_cost"],
            "dimensions": ["supplier", "ingredient", "is_direct_entry"],
            "filters": {"is_direct_entry": True},
            "order_by": [{"field": "total_cost", "direction": "desc"}],
            "limit": 10,
        }
    )

    sql = compiled["sql"]
    assert "FROM tenant_purchase_items tpi" in sql
    assert "JOIN tenant_purchases tp ON tpi.purchase_id = tp.id" in sql
    assert "LEFT JOIN tenant_suppliers ts ON tp.supplier_id = ts.id AND ts.tenant_id = tp.tenant_id" in sql
    assert "tp.tenant_id = $1" in sql
    assert "tp.is_direct_entry = $2" in sql
    assert "True" not in sql
    assert "LIMIT $3" in sql
    assert compiled["params"] == [True, 10]


def test_compile_new_queryspec_datasets_reject_invalid_surface():
    with pytest.raises(ValidationError) as invalid_field:
        queries_service.compile_queryspec(
            {
                "dataset": "inventory_stock",
                "measures": ["current_stock"],
                "dimensions": ["raw_table"],
                "filters": {},
                "order_by": [{"field": "current_stock", "direction": "desc"}],
                "limit": 10,
            }
        )
    assert "dimensions" in str(invalid_field.value.details)

    with pytest.raises(ValidationError) as invalid_bool:
        queries_service.compile_queryspec(
            {
                "dataset": "purchase_items",
                "measures": ["total_cost"],
                "dimensions": ["supplier"],
                "filters": {"is_direct_entry": "true"},
                "order_by": [{"field": "total_cost", "direction": "desc"}],
                "limit": 10,
            }
        )
    assert "filters.is_direct_entry" in str(invalid_bool.value.details)

    with pytest.raises(ValidationError) as invalid_order:
        queries_service.compile_queryspec(
            {
                "dataset": "inventory_movements",
                "measures": ["movement_count"],
                "dimensions": ["ingredient"],
                "filters": {},
                "order_by": [{"field": "net_quantity", "direction": "desc"}],
                "limit": 10,
            }
        )
    assert "order_by.0.field" in str(invalid_order.value.details)


@pytest.mark.asyncio
async def test_run_queryspec_passes_tenant_and_returns_normalized_rows():
    fake_row = {
        "product": "Test Burger",
        "revenue": Decimal("200000"),
    }

    with (
        patch(
            "app.services.queries_service.validate_api_key_auth",
            return_value=(TENANT_ID, "token-1"),
        ) as auth,
        patch(
            "app.services.queries_service.get_db_connection",
            return_value=_ConnCtx([fake_row]),
        ),
    ):
        result = await queries_service.run_queryspec(
            _request(),
            {
                "dataset": "sales_items",
                "measures": ["revenue"],
                "dimensions": ["product"],
                "filters": {},
                "order_by": [{"field": "revenue", "direction": "desc"}],
                "limit": 10,
            },
        )

    auth.assert_called_once()
    fetch_args = _ConnCtx.latest_conn.fetch.await_args.args
    assert fetch_args[1] == UUID(TENANT_ID)
    assert fetch_args[-1] == 10
    assert result["data"]["rows"] == [{"product": "Test Burger", "revenue": 200000.0}]
    assert result["data"]["meta"]["dataset"] == "sales_items"

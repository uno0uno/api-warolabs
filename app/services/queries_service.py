"""
Safe QuerySpec execution for public analytical queries.

This module exposes semantic datasets, not SQL. Clients can only reference
allowlisted dataset fields; SQL fragments remain owned by the API.
"""
import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, date
from typing import Any, Optional
from uuid import UUID

from app.core.exceptions import APIError, AuthenticationError, AuthorizationError, ValidationError
from app.database import get_db_connection
from app.services.analytics_service import _PRODUCT_COSTS_CTE
from app.services.public_api_service import POS_LIKE_FILTER_ALIAS_O, validate_api_key_auth


DEFAULT_TIMEOUT_SECONDS = 5
DEFAULT_TIMEZONE = "America/Bogota"
MAX_LIMIT = 100


@dataclass(frozen=True)
class QueryField:
    expression: str
    type: str
    label: str
    groupable: bool = False


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    label: str
    description: str
    required_scope: str
    dimensions: dict[str, QueryField]
    measures: dict[str, QueryField]
    filters: dict[str, dict[str, Any]]
    sortable: set[str]
    from_sql: str
    base_conditions: tuple[str, ...]
    cte_sql: Optional[str] = None
    extra_group_by: tuple[str, ...] = ()


DATASETS: dict[str, DatasetSpec] = {
    "sales_items": DatasetSpec(
        name="sales_items",
        label="Sales items",
        description="Completed POS/manual/table order items grouped by product, category, or day.",
        required_scope="orders:read",
        dimensions={
            "product": QueryField("p.name", "string", "Product", groupable=True),
            "product_id": QueryField("p.id", "uuid", "Product ID", groupable=True),
            "category": QueryField("c.name", "string", "Category", groupable=True),
            "day": QueryField("DATE(o.order_date AT TIME ZONE {tz})", "date", "Day", groupable=True),
        },
        measures={
            "quantity_sold": QueryField("COALESCE(SUM(oi.quantity), 0)", "number", "Quantity sold"),
            "revenue": QueryField("COALESCE(SUM(COALESCE(oi.net_total, oi.subtotal)), 0)", "currency", "Revenue"),
            "orders_count": QueryField("COUNT(DISTINCT o.id)", "integer", "Orders count"),
            "avg_price": QueryField("COALESCE(AVG(oi.price_at_purchase), 0)", "currency", "Average price"),
        },
        filters={
            "date_range": {"type": "date_range", "field": "o.order_date"},
            "category": {"type": "string", "field": "c.name"},
            "product": {"type": "string", "field": "p.name"},
        },
        sortable={"product", "category", "day", "quantity_sold", "revenue", "orders_count", "avg_price"},
        from_sql="""
            FROM order_items oi
            JOIN orders o ON oi.order_id = o.id
            JOIN product p ON oi.product_id = p.id
            LEFT JOIN categories c ON p.category_id = c.id
        """,
        base_conditions=("o.tenant_id = $1", POS_LIKE_FILTER_ALIAS_O, "o.status = 'completed'"),
    ),
    "customers": DatasetSpec(
        name="customers",
        label="Customers",
        description="Identified customer purchase and frequency rows.",
        required_scope="customers:read",
        dimensions={
            "customer": QueryField("COALESCE(p.name, 'Sin identificar')", "string", "Customer", groupable=True),
            "customer_id": QueryField("o.customer_id", "uuid", "Customer ID", groupable=True),
        },
        measures={
            "order_count": QueryField("COUNT(o.id)", "integer", "Order count"),
            "total_spent": QueryField("COALESCE(SUM(o.total_amount), 0)", "currency", "Total spent"),
            "avg_ticket": QueryField("COALESCE(AVG(o.total_amount), 0)", "currency", "Average ticket"),
            "last_order_date": QueryField("MAX(o.order_date)", "datetime", "Last order date"),
            "waros_balance": QueryField("COALESCE(MAX(ww.current_balance), 0)", "integer", "WaRos balance"),
        },
        filters={
            "date_range": {"type": "date_range", "field": "o.order_date"},
            "customer": {"type": "string", "field": "p.name"},
        },
        sortable={"customer", "order_count", "total_spent", "avg_ticket", "last_order_date", "waros_balance"},
        from_sql="""
            FROM orders o
            LEFT JOIN profile p ON o.customer_id = p.id
            LEFT JOIN waros_wallets ww ON ww.profile_id = o.customer_id AND ww.tenant_id = o.tenant_id
        """,
        base_conditions=(
            "o.tenant_id = $1",
            POS_LIKE_FILTER_ALIAS_O,
            "o.status = 'completed'",
            "o.customer_id IS NOT NULL",
        ),
    ),
    "product_profitability": DatasetSpec(
        name="product_profitability",
        label="Product profitability",
        description="Product sales, cost, profit, and margin rows using current analytics cost semantics.",
        required_scope="analytics:read",
        dimensions={
            "product": QueryField("p.name", "string", "Product", groupable=True),
            "product_id": QueryField("p.id", "uuid", "Product ID", groupable=True),
            "category": QueryField("c.name", "string", "Category", groupable=True),
            "cost_source": QueryField(
                "pc.cost_used_for_classification",
                "string",
                "Cost source",
                groupable=True,
            ),
            "classification": QueryField(
                "CASE "
                "WHEN COALESCE(SUM(oi.quantity), 0) >= 20 "
                "AND ((pc.price - pc.effective_cost) / NULLIF(pc.price, 0) * 100) >= 70 THEN 'Star' "
                "WHEN COALESCE(SUM(oi.quantity), 0) >= 20 THEN 'Plowhorse' "
                "WHEN ((pc.price - pc.effective_cost) / NULLIF(pc.price, 0) * 100) >= 70 THEN 'Puzzle' "
                "ELSE 'Dog' END",
                "string",
                "Classification",
                groupable=False,
            ),
        },
        measures={
            "quantity_sold": QueryField("COALESCE(SUM(oi.quantity), 0)", "number", "Quantity sold"),
            "revenue": QueryField("COALESCE(SUM(COALESCE(oi.net_total, oi.subtotal)), 0)", "currency", "Revenue"),
            "profit_per_unit": QueryField("(pc.price - pc.effective_cost)", "currency", "Profit per unit"),
            "profit_margin_pct": QueryField("((pc.price - pc.effective_cost) / NULLIF(pc.price, 0) * 100)", "percent", "Profit margin"),
            "profit_margin_real_pct": QueryField(
                "CASE WHEN pc.estimated_cost > 0 THEN ((pc.price - pc.estimated_cost) / pc.estimated_cost * 100) END",
                "percent",
                "Real margin",
            ),
            "profit_margin_operativo_pct": QueryField(
                "CASE WHEN pc.costo_percibido IS NOT NULL AND pc.costo_percibido > 0 "
                "THEN ((pc.price - pc.costo_percibido) / pc.costo_percibido * 100) END",
                "percent",
                "Operativo margin",
            ),
            "total_profit": QueryField("((pc.price - pc.effective_cost) * COALESCE(SUM(oi.quantity), 0))", "currency", "Total profit"),
        },
        filters={
            "date_range": {"type": "date_range", "field": "o.order_date"},
            "category": {"type": "string", "field": "c.name"},
            "product": {"type": "string", "field": "p.name"},
        },
        sortable={
            "product",
            "category",
            "cost_source",
            "classification",
            "quantity_sold",
            "revenue",
            "profit_per_unit",
            "profit_margin_pct",
            "profit_margin_real_pct",
            "profit_margin_operativo_pct",
            "total_profit",
        },
        from_sql=f"""
            FROM product p
            JOIN product_costs pc ON p.id = pc.id
            LEFT JOIN categories c ON p.category_id = c.id
            JOIN order_items oi ON p.id = oi.product_id
            JOIN orders o ON oi.order_id = o.id
        """,
        base_conditions=("p.tenant_id = $1", "o.tenant_id = $1", POS_LIKE_FILTER_ALIAS_O, "o.status = 'completed'"),
        cte_sql=_PRODUCT_COSTS_CTE,
        extra_group_by=("pc.price", "pc.effective_cost", "pc.estimated_cost", "pc.costo_percibido", "pc.cost_used_for_classification"),
    ),
}


def get_queries_schema() -> dict[str, Any]:
    return {
        "success": True,
        "data": {
            "datasets": [
                {
                    "name": dataset.name,
                    "label": dataset.label,
                    "description": dataset.description,
                    "required_scope": dataset.required_scope,
                    "default_limit": 20,
                    "max_limit": MAX_LIMIT,
                    "dimensions": _schema_fields(dataset.dimensions),
                    "measures": _schema_fields(dataset.measures),
                    "filters": dataset.filters,
                    "sortable_fields": sorted(dataset.sortable),
                }
                for dataset in DATASETS.values()
            ]
        },
    }


async def run_queryspec(request, spec: dict[str, Any]) -> dict[str, Any]:
    dataset = _get_dataset(spec.get("dataset"))
    tenant_id, _ = validate_api_key_auth(request, dataset.required_scope)
    print(
        "[api:queryspec] received "
        + json.dumps(
            {
                "tenant_id": tenant_id,
                "dataset": spec.get("dataset"),
                "measures": spec.get("measures"),
                "dimensions": spec.get("dimensions"),
                "filters": spec.get("filters"),
                "order_by": spec.get("order_by"),
                "limit": spec.get("limit"),
            },
            ensure_ascii=False,
            default=str,
        ),
        flush=True,
    )
    compiled = compile_queryspec(spec)
    print(
        "[api:queryspec] compiled "
        + json.dumps(
            {
                "dataset": dataset.name,
                "dimensions": compiled["dimensions"],
                "measures": compiled["measures"],
                "columns": [column["name"] for column in compiled["columns"]],
                "order_by": compiled["order_by"],
                "limit": compiled["limit"],
                "params_count": len(compiled["params"]),
            },
            ensure_ascii=False,
            default=str,
        ),
        flush=True,
    )

    try:
        async with get_db_connection(use_transaction=False) as conn:
            rows = await asyncio.wait_for(
                conn.fetch(compiled["sql"], UUID(tenant_id), *compiled["params"]),
                timeout=DEFAULT_TIMEOUT_SECONDS,
            )
    except asyncio.TimeoutError as exc:
        raise APIError("QuerySpec execution timed out", status_code=504) from exc
    except (AuthenticationError, AuthorizationError, ValidationError):
        raise
    except Exception as exc:
        raise APIError(f"Error executing QuerySpec: {str(exc)}", status_code=500) from exc

    return {
        "success": True,
        "data": {
            "rows": [_normalize_row(row, compiled["columns"]) for row in rows],
            "columns": compiled["columns"],
            "meta": {
                "dataset": dataset.name,
                "measures": compiled["measures"],
                "dimensions": compiled["dimensions"],
                "filters": spec.get("filters") or {},
                "order_by": compiled["order_by"],
                "limit": compiled["limit"],
                "row_count": len(rows),
            },
            "limitations": [],
        },
    }


def compile_queryspec(spec: dict[str, Any]) -> dict[str, Any]:
    dataset = _get_dataset(spec.get("dataset"))
    dimensions = _validate_fields(dataset, spec.get("dimensions") or [], "dimensions")
    measures = _validate_fields(dataset, spec.get("measures") or [], "measures")

    if not measures:
        raise ValidationError("QuerySpec must include at least one measure", {"field": "measures"})
    if dataset.name == "product_profitability" and not {"product", "product_id"}.intersection(dimensions):
        raise ValidationError(
            "product_profitability QuerySpec must include product or product_id dimension",
            {"field": "dimensions", "required_any": ["product", "product_id"]},
        )

    limit = spec.get("limit")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > MAX_LIMIT:
        raise ValidationError("QuerySpec limit must be an integer between 1 and 100", {"field": "limit", "max": MAX_LIMIT})

    timezone = spec.get("timezone") or DEFAULT_TIMEZONE
    select_parts: list[str] = []
    group_parts: list[str] = []
    columns: list[dict[str, str]] = []

    for name in dimensions:
        field = dataset.dimensions[name]
        expression = _render_expression(field.expression, timezone)
        select_parts.append(f"{expression} AS {name}")
        if field.groupable:
            group_parts.append(expression)
        columns.append({"name": name, "type": field.type, "role": "dimension", "label": field.label})

    for name in measures:
        field = dataset.measures[name]
        expression = _render_expression(field.expression, timezone)
        select_parts.append(f"{expression} AS {name}")
        columns.append({"name": name, "type": field.type, "role": "measure", "label": field.label})

    if not select_parts:
        raise ValidationError("QuerySpec must include dimensions or measures")

    params: list[Any] = []
    where_parts = list(dataset.base_conditions)
    _apply_filters(dataset, spec.get("filters") or {}, where_parts, params, timezone)

    selected_fields = set(dimensions) | set(measures)
    order_by = _validate_order_by(dataset, spec.get("order_by"), measures[0], selected_fields)
    params.append(limit)
    limit_param = len(params) + 1

    sql_parts = []
    if dataset.cte_sql:
        sql_parts.append("WITH " + dataset.cte_sql)
    sql_parts.extend(["SELECT", "    " + ",\n    ".join(select_parts), dataset.from_sql, "WHERE " + " AND ".join(where_parts)])
    if group_parts:
        sql_parts.append("GROUP BY " + ", ".join([*group_parts, *dataset.extra_group_by]))
    sql_parts.append("ORDER BY " + ", ".join(_order_sql(item) for item in order_by))
    sql_parts.append(f"LIMIT ${limit_param}")

    return {
        "sql": "\n".join(sql_parts),
        "params": params,
        "columns": columns,
        "dimensions": dimensions,
        "measures": measures,
        "order_by": order_by,
        "limit": limit,
    }


def _schema_fields(fields: dict[str, QueryField]) -> list[dict[str, str]]:
    return [{"name": name, "type": field.type, "label": field.label} for name, field in fields.items()]


def _get_dataset(name: Any) -> DatasetSpec:
    if not isinstance(name, str) or name not in DATASETS:
        raise ValidationError("Unknown QuerySpec dataset", {"field": "dataset", "allowed": sorted(DATASETS)})
    return DATASETS[name]


def _validate_fields(dataset: DatasetSpec, values: Any, attr: str) -> list[str]:
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ValidationError(f"QuerySpec {attr} must be a list of field names", {"field": attr})

    allowed = getattr(dataset, attr)
    invalid = [value for value in values if value not in allowed]
    if invalid:
        raise ValidationError(f"Invalid QuerySpec {attr}", {"field": attr, "invalid": invalid, "allowed": sorted(allowed)})

    return values


def _apply_filters(
    dataset: DatasetSpec,
    filters: Any,
    where_parts: list[str],
    params: list[Any],
    timezone: str,
) -> None:
    if not isinstance(filters, dict):
        raise ValidationError("QuerySpec filters must be an object", {"field": "filters"})

    invalid = [key for key in filters if key not in dataset.filters]
    if invalid:
        raise ValidationError("Invalid QuerySpec filters", {"field": "filters", "invalid": invalid, "allowed": sorted(dataset.filters)})

    date_range = filters.get("date_range")
    if date_range is not None:
        if not isinstance(date_range, dict):
            raise ValidationError("date_range filter must be an object", {"field": "filters.date_range"})
        date_field = dataset.filters["date_range"]["field"]
        start = _parse_date(date_range.get("from"), "filters.date_range.from")
        end = _parse_date(date_range.get("to"), "filters.date_range.to")
        if start:
            params.extend([start, timezone])
            where_parts.append(f"{date_field} >= (${len(params)}::timestamp AT TIME ZONE ${len(params) + 1})")
        if end:
            params.extend([end, timezone])
            where_parts.append(f"{date_field} < ((${len(params)}::timestamp + interval '1 day') AT TIME ZONE ${len(params) + 1})")

    for key, value in filters.items():
        if key == "date_range" or value is None:
            continue
        field = dataset.filters[key]["field"]
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"{key} filter must be a non-empty string", {"field": f"filters.{key}"})
        params.append(value.strip())
        where_parts.append(f"{field} = ${len(params) + 1}")


def _validate_order_by(
    dataset: DatasetSpec,
    order_by: Any,
    default_field: str,
    selected_fields: set[str],
) -> list[dict[str, str]]:
    if order_by is None:
        order_by = [{"field": default_field, "direction": "desc"}]
    if not isinstance(order_by, list) or not order_by:
        raise ValidationError("QuerySpec order_by must be a non-empty list", {"field": "order_by"})

    normalized = []
    for index, item in enumerate(order_by):
        if not isinstance(item, dict):
            raise ValidationError("QuerySpec order_by entries must be objects", {"field": f"order_by.{index}"})
        field = item.get("field")
        direction = (item.get("direction") or "asc").lower()
        if field not in dataset.sortable:
            raise ValidationError("Invalid QuerySpec order_by field", {"field": f"order_by.{index}.field", "invalid": field, "allowed": sorted(dataset.sortable)})
        if field not in selected_fields:
            raise ValidationError(
                "QuerySpec order_by field must be selected as a dimension or measure",
                {"field": f"order_by.{index}.field", "invalid": field, "selected": sorted(selected_fields)},
            )
        if direction not in {"asc", "desc"}:
            raise ValidationError("Invalid QuerySpec order_by direction", {"field": f"order_by.{index}.direction", "allowed": ["asc", "desc"]})
        normalized.append({"field": field, "direction": direction})
    return normalized


def _order_sql(item: dict[str, str]) -> str:
    direction = "ASC" if item["direction"] == "asc" else "DESC"
    return f"{item['field']} {direction} NULLS LAST"


def _parse_date(value: Any, field: str) -> Optional[date]:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValidationError("Date filter values must be YYYY-MM-DD strings", {"field": field})
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValidationError("Date filter values must be YYYY-MM-DD strings", {"field": field}) from exc


def _render_expression(expression: str, timezone: str) -> str:
    return expression.replace("{tz}", _quote_literal(timezone))


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _normalize_row(row, columns: list[dict[str, str]]) -> dict[str, Any]:
    result = {}
    for column in columns:
        value = row[column["name"]]
        if isinstance(value, UUID):
            value = str(value)
        elif hasattr(value, "isoformat"):
            value = value.isoformat()
        elif value is not None and column["type"] in {"number", "currency", "percent"}:
            value = float(value)
        result[column["name"]] = value
    return result

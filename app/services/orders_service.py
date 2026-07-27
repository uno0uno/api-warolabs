"""
Orders Service
Handles listing and filtering of POS orders
"""
import asyncio
from decimal import Decimal
from typing import Any, Dict, Optional, List
from uuid import UUID
from fastapi import Request
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError, APIError
from app.core.timezones import get_zoneinfo, local_date_for_tenant, resolve_tenant_timezone, tenant_today
from app.core.localization import (
    format_datetime as format_localized_datetime,
    format_money,
    get_translator,
    resolve_tenant_locale_settings,
)
from app.services.aws_ses_service import ses_service
from app.services.waros_service import evaluate_and_award
from app.services.cierre_service import (
    assert_order_not_in_closed_monthly_period,
    _get_tenant_tax_config,
    _post_order_cogs_gl_entry,
    _post_order_gl_entry,
)
from app.services.account_role_service import (
    AccountRole,
    MissingAccountRoleError,
    resolve_account,
)
from app.services.email_helpers import send_pos_receipt_email
from app.services import invoice_email_tracking_service
from app.services.email_sender import resolve_sender_email_for_tenant
from app.services.invoicing_presentation import (
    build_invoice_presentation,
    commercial_header_name,
)
from app.services.modifier_option_service import (
    modifier_line_subtotal,
    resolve_modifier_selections,
)
from fastapi import HTTPException
from datetime import datetime, date, timezone
import csv


import io
import logging


def parse_date(date_str: Optional[str]) -> Optional[date]:
    """Convert date string (YYYY-MM-DD) to date object"""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return None

logger = logging.getLogger(__name__)
_INVENTORY_QUANTITY_SCALE = Decimal("0.000001")


def _tr(_, message: str, **kwargs: Any) -> str:
    return _(message).format(**kwargs)


def _date_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    return value.isoformat()


def _append_local_date_bounds(
    where_conditions: List[str],
    params: List[Any],
    param_count: int,
    column: str,
    parsed_date_from: Optional[date],
    parsed_date_to: Optional[date],
    timezone_name: str,
) -> int:
    if parsed_date_from:
        param_count += 1
        date_param = param_count
        param_count += 1
        tz_param = param_count
        where_conditions.append(f"{column} >= (${date_param}::timestamp AT TIME ZONE ${tz_param})")
        params.extend([parsed_date_from, timezone_name])

    if parsed_date_to:
        param_count += 1
        date_param = param_count
        param_count += 1
        tz_param = param_count
        where_conditions.append(
            f"{column} < ((${date_param}::timestamp + interval '1 day') AT TIME ZONE ${tz_param})"
        )
        params.extend([parsed_date_to, timezone_name])

    return param_count


def _inventory_quantity(value: Any) -> float:
    quantized = Decimal(str(value)).quantize(_INVENTORY_QUANTITY_SCALE)
    if quantized == 0:
        quantized = Decimal("0")
    return float(quantized)

def _pos_modifier_inventory_helpers():
    from app.services.pos_cart_service import (
        _deduct_modifier_inventory_for_order_item,
        return_modifier_inventory_for_order_item,
        return_order_item_inventory_from_snapshots,
    )
    return (
        _deduct_modifier_inventory_for_order_item,
        return_modifier_inventory_for_order_item,
        return_order_item_inventory_from_snapshots,
    )


def _pos_order_item_ingredient_snapshot_helper():
    from app.services.pos_cart_service import _capture_order_item_ingredients

    return _capture_order_item_ingredients



POS_LIKE_FILTER = (
    "(pos_cart_id IS NOT NULL OR table_session_id IS NOT NULL "
    "OR extra_attributes->>'source' = 'manual')"
)
POS_LIKE_FILTER_ALIAS_O = (
    "(o.pos_cart_id IS NOT NULL OR o.table_session_id IS NOT NULL "
    "OR o.extra_attributes->>'source' = 'manual')"
)
ANALYTICS_SALES_FILTER = (
    "(pos_cart_id IS NOT NULL OR table_session_id IS NOT NULL "
    "OR online_cart_id IS NOT NULL "
    "OR extra_attributes->>'source' = 'manual')"
)
ANALYTICS_SALES_FILTER_ALIAS_O = (
    "(o.pos_cart_id IS NOT NULL OR o.table_session_id IS NOT NULL "
    "OR o.online_cart_id IS NOT NULL "
    "OR o.extra_attributes->>'source' = 'manual')"
)


async def _get_order_promo_summary(conn, order_id: UUID) -> Dict[str, Any]:
    """Aggregate persisted line promos for order detail / reporting (warocol.com#984)."""
    rows = await conn.fetch(
        """
        SELECT
            oi.applied_promotion_id,
            tp.name AS promotion_name,
            tp.promo_type,
            COALESCE(SUM(oi.promo_savings_allocated), 0) AS savings
        FROM order_items oi
        INNER JOIN tenant_promotions tp ON tp.id = oi.applied_promotion_id
        WHERE oi.order_id = $1
          AND oi.applied_promotion_id IS NOT NULL
        GROUP BY oi.applied_promotion_id, tp.name, tp.promo_type
        ORDER BY savings DESC
        """,
        order_id,
    )
    breakdown = [
        {
            "promotion_id": str(r["applied_promotion_id"]),
            "promotion_name": r["promotion_name"],
            "promo_type": r["promo_type"],
            "savings": float(r["savings"]),
        }
        for r in rows
    ]
    promo_savings = sum(item["savings"] for item in breakdown)
    return {"promo_savings": promo_savings, "promo_breakdown": breakdown}


async def _get_order_waro_redemption_summary(conn, order_id: UUID) -> Dict[str, Any]:
    """Aggregate persisted WaRo redemptions for order detail / receipts (api-warolabs#375)."""
    rows = await conn.fetch(
        """
        SELECT
            owr.redemption_type,
            owr.waros_spent,
            owr.cop_discount,
            owr.waro_reward_id,
            wr.name AS reward_name
        FROM order_waro_redemptions owr
        LEFT JOIN waro_rewards wr ON wr.id = owr.waro_reward_id
        WHERE owr.order_id = $1
        ORDER BY owr.created_at
        """,
        order_id,
    )
    breakdown = [
        {
            "redemption_type": r["redemption_type"],
            "waros_spent": int(r["waros_spent"]),
            "cop_discount": float(r["cop_discount"]),
            "waro_reward_id": str(r["waro_reward_id"]) if r["waro_reward_id"] else None,
            "reward_name": r["reward_name"],
        }
        for r in rows
    ]
    waro_discount_cop = sum(item["cop_discount"] for item in breakdown)
    waros_spent = sum(item["waros_spent"] for item in breakdown)
    if breakdown:
        return {
            "waro_discount_cop": waro_discount_cop,
            "waros_spent": waros_spent,
            "waro_breakdown": breakdown,
        }

    empty = {"waro_discount_cop": 0.0, "waros_spent": 0, "waro_breakdown": []}

    # Mesa checkout sometimes applied WaRo to line net_total without persisting
    # order_waro_redemptions (e.g. close before settle). Infer for display only.
    inferred_row = await conn.fetchrow(
        """
        SELECT COALESCE(SUM(
            GREATEST(0, oi.subtotal - COALESCE(oi.net_total, oi.subtotal))
        ), 0) AS waro_discount_cop
        FROM order_items oi
        WHERE oi.order_id = $1
          AND oi.applied_promotion_id IS NULL
        """,
        order_id,
    )
    inferred_cop = float(inferred_row["waro_discount_cop"] or 0) if inferred_row else 0.0
    if inferred_cop <= 0:
        return empty

    order_discount = await conn.fetchval(
        "SELECT COALESCE(discount_amount, 0) FROM orders WHERE id = $1",
        order_id,
    )
    if float(order_discount or 0) > 0:
        return empty

    has_promo_lines = await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM order_items
            WHERE order_id = $1 AND applied_promotion_id IS NOT NULL
        )
        """,
        order_id,
    )
    if has_promo_lines:
        return empty

    return {
        "waro_discount_cop": inferred_cop,
        "waros_spent": 0,
        "waro_breakdown": [
            {
                "redemption_type": "inferred_line_discount",
                "waros_spent": 0,
                "cop_discount": inferred_cop,
                "waro_reward_id": None,
                "reward_name": None,
            },
        ],
    }


def _compute_tax_breakdown(
    items_rows: List[Any],
    tax_config: Dict[str, Any],
) -> tuple:
    """
    Compute standard_tax, liquor_tax, and standard_tax_label from a list of rows
    with .tax_category and .subtotal fields.

    Works with both item-level rows (one row per order item) and pre-aggregated
    rows (one row per tax_category, subtotal already summed by SQL).

    Returns: (standard_tax: float, liquor_tax: float, standard_tax_label: str)
    """
    from app.services.hospitality_tax_engine import compute_category_breakdown

    return compute_category_breakdown(items_rows, tax_config)


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if not row:
        return default
    if hasattr(row, "get"):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def _iso_datetime(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _invoice_attachment_flags(invoice_row: Any) -> Dict[str, bool]:
    return {
        "pdf": bool(_row_get(invoice_row, "r2_pdf_key")),
        "xml": bool(_row_get(invoice_row, "r2_xml_key")),
    }


def _tax_detail_rows(
    items_rows: List[Any],
    standard_tax: float,
    liquor_tax: float,
    standard_tax_label: str,
    tax_config: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    std_base = sum(float(r["subtotal"]) for r in items_rows if r["tax_category"] == "standard")
    liq_base = sum(float(r["subtotal"]) for r in items_rows if r["tax_category"] == "liquor")
    rows: List[Dict[str, Any]] = []
    if standard_tax > 0:
        rows.append({"label": standard_tax_label, "base": std_base, "amount": standard_tax})
    if liquor_tax > 0:
        liquor_label = "IVA licores 5%"
        if tax_config is not None:
            from app.services.hospitality_tax_engine import resolve_tax_profile

            liq_line = resolve_tax_profile(tax_config).line_for_category("liquor")
            if liq_line:
                liquor_label = liq_line.label
        rows.append({"label": liquor_label, "base": liq_base, "amount": liquor_tax})
    return rows


def _build_invoice_presentation(
    invoice_row: Any,
    order_row: Any,
    profile_row: Any,
    resolution_row: Any,
    tax_details: List[Dict[str, Any]],
    *,
    serialize_datetimes: bool = False,
    provider: str = "matias",
) -> Dict[str, Any]:
    """
    FE presentation for email/print/API.

    Emisor = tenant fiscal only (never product brand / display_name as issuer).
    Facturador path defaults to Matias Casa de Software.
    """
    return build_invoice_presentation(
        invoice_row=invoice_row,
        order_row=order_row,
        fiscal_row=profile_row,
        public_profile=profile_row,
        resolution_row=resolution_row,
        tax_details=tax_details,
        provider=provider,
        serialize_datetimes=serialize_datetimes,
    )


async def get_orders_list(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    search: Optional[str] = None,
    search_field: Optional[str] = None,
    payment_method: Optional[str] = None,
    payment_method_id: Optional[str] = None,
    status: Optional[str] = None,
    sort_field: str = "order_date",
    sort_direction: str = "desc",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    delivery_only: Optional[bool] = None,
) -> dict:
    """
    Get list of POS orders with filters and pagination
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            timezone_name = await resolve_tenant_timezone(conn, tenant_id)
            # Build WHERE clause
            where_conditions = [
                "o.tenant_id = $1",
                "(o.pos_cart_id IS NOT NULL OR o.table_session_id IS NOT NULL OR o.extra_attributes->>'source' = 'manual')"
            ]
            params = [tenant_id]
            param_count = 1

            # Search filter
            if search and search_field:
                param_count += 1
                if search_field == "order_number":
                    where_conditions.append(f"CAST(o.order_number AS TEXT) ILIKE ${param_count}")
                    params.append(f"%{search}%")
                elif search_field == "customer_name":
                    where_conditions.append(f"p.name ILIKE ${param_count}")
                    params.append(f"%{search}%")
                elif search_field == "customer_phone":
                    where_conditions.append(f"p.phone_number ILIKE ${param_count}")
                    params.append(f"%{search}%")

            # Payment method filter
            if payment_method:
                param_count += 1
                where_conditions.append(f"o.payment_method = ${param_count}")
                params.append(payment_method)

            # Payment method ID filter (specific custom method)
            if payment_method_id:
                param_count += 1
                where_conditions.append(f"o.payment_method_id = ${param_count}::uuid")
                params.append(payment_method_id)

            # Status filter
            if status:
                param_count += 1
                where_conditions.append(f"o.status = ${param_count}")
                params.append(status)

            # Date range filter
            parsed_date_from = parse_date(date_from)
            parsed_date_to = parse_date(date_to)
            param_count = _append_local_date_bounds(
                where_conditions,
                params,
                param_count,
                "o.order_date",
                parsed_date_from,
                parsed_date_to,
                timezone_name,
            )

            # Delivery-only filter: composes with POS_LIKE_FILTER, uses partial index idx_orders_delivery_address_id
            if delivery_only:
                where_conditions.append("o.delivery_address_id IS NOT NULL")

            where_clause = " AND ".join(where_conditions)

            # Validate sort field
            allowed_sort_fields = ["order_number", "order_date", "total_amount", "customer_name", "payment_method", "discount_amount"]
            if sort_field not in allowed_sort_fields:
                sort_field = "order_date"

            sort_direction = "ASC" if sort_direction.lower() == "asc" else "DESC"

            # Map sort field to actual column
            sort_column_map = {
                "order_number": "o.order_number",
                "order_date": "o.order_date",
                "total_amount": "o.total_amount",
                "customer_name": "p.name",
                "payment_method": "o.payment_method",
                "discount_amount": "o.discount_amount"
            }
            sort_column = sort_column_map.get(sort_field, "o.order_date")

            # Single query: COUNT(*) OVER() replaces separate count round-trip.
            # items_count uses correlated subquery (idx_order_items_order_id exists).
            param_count += 1
            limit_param = param_count
            param_count += 1
            offset_param = param_count

            orders_query = f"""
                SELECT
                    o.id,
                    o.order_number,
                    o.order_date,
                    o.total_amount,
                    o.status,
                    o.payment_method,
                    o.payment_method_id,
                    o.payment_status,
                    o.credit_due_date,
                    o.credit_paid_amount,
                    o.discount_amount,
                    o.pos_cart_id,
                    o.table_session_id,
                    o.delivery_address_id,
                    o.scheduled_time,
                    o.delivery_instructions,
                    t_meta.is_bar as is_bar,
                    p.id as customer_id,
                    p.name as customer_name,
                    p.phone_number as customer_phone,
                    ei.id as invoice_id,
                    ei.prefix as invoice_prefix,
                    ei.invoice_number as invoice_number,
                    ei.status as invoice_status,
                    (
                        SELECT COUNT(*)
                        FROM order_items oi
                        WHERE oi.order_id = o.id
                    ) as items_count,
                    (
                        SELECT COUNT(*)
                        FROM order_payments op
                        WHERE op.order_id = o.id
                    ) as split_payments_count,
                    COUNT(*) OVER() as total_count
                FROM orders o
                LEFT JOIN profile p ON o.customer_id = p.id
                LEFT JOIN table_sessions ts_meta ON ts_meta.id = o.table_session_id
                LEFT JOIN tables t_meta ON t_meta.id = ts_meta.table_id
                LEFT JOIN LATERAL (
                    SELECT id, prefix, invoice_number, status
                    FROM electronic_invoices
                    WHERE order_id = o.id AND tenant_id = $1
                    ORDER BY created_at DESC LIMIT 1
                ) ei ON true
                WHERE {where_clause}
                ORDER BY {sort_column} {sort_direction}
                LIMIT ${limit_param} OFFSET ${offset_param}
            """

            params.extend([limit, offset])
            orders_rows = await conn.fetch(orders_query, *params)
            total_count = orders_rows[0]['total_count'] if orders_rows else 0

            orders = [
                {
                    "id": str(row['id']),
                    "order_number": int(row['order_number']),
                    "order_date": row['order_date'].isoformat(),
                    "total_amount": float(row['total_amount']),
                    "status": row['status'],
                    "payment_method": row['payment_method'],
                    "payment_method_id": str(row['payment_method_id']) if row['payment_method_id'] else None,
                    "payment_status": row['payment_status'],
                    "credit_due_date": str(row['credit_due_date']) if row['credit_due_date'] is not None else None,
                    "credit_paid_amount": float(row['credit_paid_amount']) if row['credit_paid_amount'] is not None else 0.0,
                    "discount_amount": float(row['discount_amount']) if row['discount_amount'] is not None else 0.0,
                    "pos_cart_id": str(row['pos_cart_id']) if row['pos_cart_id'] else None,
                    "source": (
                        "barra" if row['table_session_id'] and row['is_bar'] else
                        "mesa" if row['table_session_id'] else
                        "pos"
                    ),
                    "delivery_address_id": str(row['delivery_address_id']) if row['delivery_address_id'] else None,
                    "scheduled_time": row['scheduled_time'].isoformat() if row['scheduled_time'] else None,
                    "delivery_instructions": row['delivery_instructions'],
                    "is_delivery": row['delivery_address_id'] is not None,
                    "customer": {
                        "id": str(row['customer_id']) if row['customer_id'] else None,
                        "name": row['customer_name'],
                        "phone": row['customer_phone']
                    },
                    "invoice_id": str(row['invoice_id']) if row['invoice_id'] else None,
                    "invoice_prefix": row['invoice_prefix'],
                    "invoice_number": row['invoice_number'],
                    "invoice_status": row['invoice_status'],
                    "items_count": row['items_count'],
                    "split_payments_count": int(row['split_payments_count'])
                }
                for row in orders_rows
            ]

            return {
                "success": True,
                "data": orders,
                "pagination": {
                    "total": total_count,
                    "limit": limit,
                    "offset": offset,
                    "has_more": (offset + limit) < total_count
                }
            }

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting orders list: {str(e)}")
        raise APIError(f"Error getting orders list: {str(e)}", status_code=500)


async def get_tips_list(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    search: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    member_id: Optional[UUID] = None,
    payment_method: Optional[str] = None,
    channel: Optional[str] = None,
    sort_field: str = "order_date",
    sort_direction: str = "desc",
) -> dict:
    """
    Get list of orders that captured a tip, with filters, pagination, and
    aggregates. Powers /ventas/propinas (warocol.com#640).

    Base WHERE: tenant scope + tip_amount > 0. The aggregates query runs against
    the same WHERE so the totals always match the visible page.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            timezone_name = await resolve_tenant_timezone(conn, tenant_id)
            where_conditions = [
                "o.tenant_id = $1",
                "o.tip_amount > 0",
            ]
            params: List[Any] = [tenant_id]
            param_count = 1

            # Free-text search on order_number
            if search:
                param_count += 1
                where_conditions.append(f"CAST(o.order_number AS TEXT) ILIKE ${param_count}")
                params.append(f"%{search}%")

            if payment_method:
                param_count += 1
                where_conditions.append(f"o.payment_method = ${param_count}")
                params.append(payment_method)

            if member_id:
                param_count += 1
                where_conditions.append(f"o.served_by_member_id = ${param_count}::uuid")
                params.append(str(member_id))

            # channel ∈ {'online', 'mesa', 'pos'} mirrors the derivation in get_orders_list
            if channel == 'online':
                where_conditions.append("o.online_cart_id IS NOT NULL")
            elif channel == 'mesa':
                where_conditions.append("o.table_session_id IS NOT NULL")
            elif channel == 'pos':
                where_conditions.append("o.pos_cart_id IS NOT NULL AND o.table_session_id IS NULL")

            parsed_date_from = parse_date(date_from)
            parsed_date_to = parse_date(date_to)
            param_count = _append_local_date_bounds(
                where_conditions,
                params,
                param_count,
                "o.order_date",
                parsed_date_from,
                parsed_date_to,
                timezone_name,
            )

            where_clause = " AND ".join(where_conditions)

            allowed_sort_fields = ["order_number", "order_date", "total_amount", "tip_amount", "payment_method"]
            if sort_field not in allowed_sort_fields:
                sort_field = "order_date"
            sort_direction_sql = "ASC" if sort_direction.lower() == "asc" else "DESC"
            sort_column_map = {
                "order_number": "o.order_number",
                "order_date": "o.order_date",
                "total_amount": "o.total_amount",
                "tip_amount": "o.tip_amount",
                "payment_method": "o.payment_method",
            }
            sort_column = sort_column_map.get(sort_field, "o.order_date")

            param_count += 1
            limit_param = param_count
            param_count += 1
            offset_param = param_count

            tips_query = f"""
                SELECT
                    o.id,
                    o.order_number,
                    o.order_date,
                    o.total_amount,
                    o.tip_amount,
                    o.tip_source,
                    o.payment_method,
                    o.payment_method_id,
                    o.pos_cart_id,
                    o.online_cart_id,
                    o.table_session_id,
                    o.served_by_member_id,
                    p.name AS member_name,
                    t_meta.is_bar AS is_bar,
                    COUNT(*) OVER() AS total_count
                FROM orders o
                LEFT JOIN tenant_members tm ON tm.id = o.served_by_member_id
                LEFT JOIN profile p ON p.id = tm.user_id
                LEFT JOIN table_sessions ts_meta ON ts_meta.id = o.table_session_id
                LEFT JOIN tables t_meta ON t_meta.id = ts_meta.table_id
                WHERE {where_clause}
                ORDER BY {sort_column} {sort_direction_sql}
                LIMIT ${limit_param} OFFSET ${offset_param}
            """

            row_params = list(params) + [limit, offset]
            rows = await conn.fetch(tips_query, *row_params)
            total_count = rows[0]['total_count'] if rows else 0

            # Aggregates run against the same WHERE (no LIMIT/OFFSET, no pagination params)
            aggregates_query = f"""
                SELECT
                    COALESCE(SUM(o.tip_amount), 0) AS sum_tip,
                    COUNT(*) AS count_with_tip,
                    COALESCE(AVG(
                        CASE WHEN o.total_amount > 0
                             THEN o.tip_amount / o.total_amount * 100
                             ELSE 0
                        END
                    ), 0) AS avg_pct
                FROM orders o
                WHERE {where_clause}
            """
            agg_row = await conn.fetchrow(aggregates_query, *params)

            tips = [
                {
                    "id": str(row['id']),
                    "order_number": int(row['order_number']),
                    "order_date": row['order_date'].isoformat(),
                    "total_amount": float(row['total_amount']),
                    "tip_amount": float(row['tip_amount']),
                    "tip_source": row['tip_source'],
                    "tip_percent": (
                        round(float(row['tip_amount']) / float(row['total_amount']) * 100, 2)
                        if float(row['total_amount']) > 0 else 0.0
                    ),
                    "payment_method": row['payment_method'],
                    "payment_method_id": str(row['payment_method_id']) if row['payment_method_id'] else None,
                    "channel": (
                        "online" if row['online_cart_id'] else
                        "barra" if row['table_session_id'] and row['is_bar'] else
                        "mesa" if row['table_session_id'] else
                        "pos"
                    ),
                    "served_by_member_id": str(row['served_by_member_id']) if row['served_by_member_id'] else None,
                    "member_name": row['member_name'],
                }
                for row in rows
            ]

            return {
                "success": True,
                "data": tips,
                "pagination": {
                    "total": total_count,
                    "limit": limit,
                    "offset": offset,
                    "has_more": (offset + limit) < total_count,
                },
                "aggregates": {
                    "sum_tip": float(agg_row['sum_tip']) if agg_row else 0.0,
                    "count_with_tip": int(agg_row['count_with_tip']) if agg_row else 0,
                    "avg_pct": round(float(agg_row['avg_pct']), 2) if agg_row else 0.0,
                },
            }

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting tips list: {str(e)}")
        raise APIError(f"Error getting tips list: {str(e)}", status_code=500)


async def get_order_by_id(
    request: Request,
    order_id: UUID
) -> dict:
    """
    Get single order by ID with customer details
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            order_query = """
                SELECT
                    o.id,
                    o.order_number,
                    o.order_date,
                    o.total_amount,
                    o.status,
                    o.payment_method,
                    o.payment_method_id,
                    o.payment_status,
                    o.credit_due_date,
                    o.credit_paid_amount,
                    o.discount_amount,
                    o.discount_type,
                    o.discount_value,
                    o.pos_cart_id,
                    o.table_session_id,
                    o.delivery_address_id,
                    o.scheduled_time,
                    o.delivery_instructions,
                    o.tip_amount,
                    o.tip_source,
                    o.tip_tax_amount,
                    t_meta2.is_bar as is_bar,
                    o.served_by_member_id,
                    p_served.name as served_by_member_name,
                    p.id as customer_id,
                    p.name as customer_name,
                    p.phone_number as customer_phone,
                    p.email as customer_email,
                    -- Hydrated delivery address (NULL if not a delivery, or address was soft-deleted)
                    ap.address_line1   AS addr_line1,
                    ap.address_line2   AS addr_line2,
                    ap.city            AS addr_city,
                    ap.state           AS addr_state,
                    ap.postal_code     AS addr_postal_code,
                    ap.country         AS addr_country,
                    ap.latitude        AS addr_latitude,
                    ap.longitude       AS addr_longitude,
                    ap.label           AS addr_type,
                    ap.delivery_notes  AS addr_delivery_notes,
                    (
                        SELECT COUNT(*)
                        FROM order_items oi
                        WHERE oi.order_id = o.id
                    ) as items_count
                FROM orders o
                LEFT JOIN tenant_members tm_served ON tm_served.id = o.served_by_member_id
                LEFT JOIN profile p_served ON p_served.id = tm_served.user_id
                LEFT JOIN profile p ON o.customer_id = p.id
                LEFT JOIN table_sessions ts_meta2 ON ts_meta2.id = o.table_session_id
                LEFT JOIN tables t_meta2 ON t_meta2.id = ts_meta2.table_id
                LEFT JOIN addresses_profile ap ON ap.id = o.delivery_address_id AND ap.deleted_at IS NULL
                WHERE o.id = $1 AND o.tenant_id = $2
                  AND (o.pos_cart_id IS NOT NULL OR o.table_session_id IS NOT NULL OR o.extra_attributes->>'source' = 'manual')

            """

            order_row = await conn.fetchrow(order_query, order_id, tenant_id)

            if not order_row:
                raise APIError("Order not found", status_code=404)

            # Fetch split payments if any
            payments_rows = await conn.fetch(
                """
                SELECT id, amount, payment_method, payment_method_id, paid_at
                FROM order_payments
                WHERE order_id = $1
                ORDER BY paid_at ASC
                """,
                order_id
            )
            split_payments = [
                {
                    "id": str(r['id']),
                    "amount": float(r['amount']),
                    "payment_method": r['payment_method'],
                    "payment_method_id": str(r['payment_method_id']) if r['payment_method_id'] else None,
                    "paid_at": r['paid_at'].isoformat(),
                }
                for r in payments_rows
            ]

            # Tax breakdown
            _std_tax = 0.0
            _liq_tax = 0.0
            _tax_label = "Impuesto"
            try:
                tax_config = await _get_tenant_tax_config(conn, tenant_id)
                items_rows = await conn.fetch(
                    """SELECT COALESCE(p.tax_category, 'standard') AS tax_category,
                              COALESCE(oi.net_total, oi.subtotal, 0) AS subtotal
                       FROM order_items oi
                       JOIN product p ON p.id = oi.product_id
                       WHERE oi.order_id = $1""",
                    order_id
                )
                _std_tax, _liq_tax, _tax_label = _compute_tax_breakdown(items_rows, tax_config)
            except Exception as _e:
                logger.warning(f"Tax breakdown failed for order {order_id}: {_e}")

            _promo_summary = await _get_order_promo_summary(conn, order_id)
            _waro_summary = await _get_order_waro_redemption_summary(conn, order_id)
            advance_account = await resolve_account(
                conn,
                tenant_id,
                AccountRole.CUSTOMER_ADVANCES,
                required=False,
                source="order_detail",
            )
            advance_gl_row = None
            if advance_account:
                advance_gl_row = await conn.fetchrow(
                    """
                    SELECT COALESCE(SUM(jl.debit), 0) AS advance_applied
                    FROM tenant_journal_entries je
                    JOIN tenant_journal_lines jl ON jl.journal_entry_id = je.id
                    WHERE je.tenant_id = $1
                      AND je.source_module = 'orden'
                      AND je.source_id = $2
                      AND je.status = 'posted'
                      AND jl.account_id = $3
                      AND jl.debit > 0
                      AND jl.description ILIKE '%anticipo mesa%'
                    """,
                    tenant_id,
                    order_id,
                    advance_account.id,
                )
            _tip_amount = float(order_row['tip_amount']) if order_row['tip_amount'] is not None else 0.0
            _tip_tax_amount = float(order_row['tip_tax_amount']) if order_row['tip_tax_amount'] is not None else 0.0
            _settlement_amount = float(order_row['total_amount']) + _tip_amount + _tip_tax_amount
            _advance_applied = float(advance_gl_row["advance_applied"] or 0) if advance_gl_row else 0.0
            if _advance_applied <= 0:
                advance_direct_row = await conn.fetchrow(
                    """
                    SELECT COALESCE(SUM(applied_amount_cop), 0) AS advance_applied
                    FROM table_session_advances
                    WHERE tenant_id = $1
                      AND status = 'active'
                      AND $2 = ANY(applied_order_ids)
                      AND cardinality(applied_order_ids) = 1
                    """,
                    tenant_id,
                    order_id,
                )
                _advance_applied = (
                    float(advance_direct_row["advance_applied"] or 0)
                    if advance_direct_row else 0.0
                )
            _advance_applied = min(_settlement_amount, _advance_applied)
            _charged_amount = None
            if _tip_amount > 0 or _advance_applied > 0:
                _charged_amount = max(
                    0.0,
                    _settlement_amount - _advance_applied,
                )

            # Hydrate delivery address inline (None if not a delivery, or address was soft-deleted)
            delivery_address = None
            if order_row['delivery_address_id'] and order_row['addr_line1'] is not None:
                delivery_address = {
                    "id": str(order_row['delivery_address_id']),
                    "address_line1": order_row['addr_line1'],
                    "address_line2": order_row['addr_line2'],
                    "city": order_row['addr_city'],
                    "state": order_row['addr_state'],
                    "postal_code": order_row['addr_postal_code'],
                    "country": order_row['addr_country'],
                    "latitude": float(order_row['addr_latitude']) if order_row['addr_latitude'] is not None else None,
                    "longitude": float(order_row['addr_longitude']) if order_row['addr_longitude'] is not None else None,
                    "address_type": order_row['addr_type'],
                    "delivery_notes": order_row['addr_delivery_notes'],
                }

            return {
                "success": True,
                "data": {
                    "id": str(order_row['id']),
                    "order_number": int(order_row['order_number']),
                    "order_date": order_row['order_date'].isoformat(),
                    "total_amount": float(order_row['total_amount']),
                    "status": order_row['status'],
                    "payment_method": order_row['payment_method'],
                    "payment_method_id": str(order_row['payment_method_id']) if order_row['payment_method_id'] else None,
                    "payment_status": order_row['payment_status'],
                    "credit_due_date": str(order_row['credit_due_date']) if order_row['credit_due_date'] is not None else None,
                    "credit_paid_amount": float(order_row['credit_paid_amount']) if order_row['credit_paid_amount'] is not None else 0.0,
                    "discount_amount": float(order_row['discount_amount']) if order_row['discount_amount'] is not None else 0.0,
                    "discount_type": order_row['discount_type'],
                    "discount_value": float(order_row['discount_value']) if order_row['discount_value'] is not None else None,
                    "pos_cart_id": str(order_row['pos_cart_id']) if order_row['pos_cart_id'] else None,
                    "source": (
                        "barra" if order_row['table_session_id'] and order_row['is_bar'] else
                        "mesa" if order_row['table_session_id'] else
                        "pos"
                    ),
                    "delivery_address_id": str(order_row['delivery_address_id']) if order_row['delivery_address_id'] else None,
                    "delivery_address": delivery_address,
                    "scheduled_time": order_row['scheduled_time'].isoformat() if order_row['scheduled_time'] else None,
                    "delivery_instructions": order_row['delivery_instructions'],
                    "is_delivery": order_row['delivery_address_id'] is not None,
                    "customer": {
                        "id": str(order_row['customer_id']) if order_row['customer_id'] else None,
                        "name": order_row['customer_name'],
                        "phone": order_row['customer_phone'],
                        "email": order_row['customer_email'],
                    },
                    "served_by_member_id": str(order_row['served_by_member_id']) if order_row['served_by_member_id'] else None,
                    "served_by_member_name": order_row['served_by_member_name'],
                    "tip_amount": _tip_amount,
                    "tip_source": order_row['tip_source'] or 'none',
                    "tip_tax_amount": _tip_tax_amount,
                    "advance_applied": _advance_applied,
                    "charged_amount": _charged_amount,
                    "items_count": order_row['items_count'],
                    "split_payments": split_payments,
                    "standard_tax": _std_tax,
                    "liquor_tax": _liq_tax,
                    "standard_tax_label": _tax_label,
                    "promo_savings": _promo_summary["promo_savings"],
                    "promo_breakdown": _promo_summary["promo_breakdown"],
                    "waro_redemption_summary": _waro_summary,
                }
            }

    except (AuthenticationError, APIError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting order by ID: {str(e)}")
        raise APIError(f"Error getting order by ID: {str(e)}", status_code=500)


async def bulk_update_order_status(
    request: Request,
    order_ids: list,
    status: str,
    payment_method: Optional[str] = None,
    customer_id: Optional[str] = None,
    payment_method_id: Optional[str] = None,
) -> dict:
    """Bulk update status for multiple orders belonging to the tenant."""
    allowed = {"completed", "cancelled", "pending"}
    if status not in allowed:
        raise APIError(f"Estado inválido. Valores permitidos: {', '.join(allowed)}", status_code=400)

    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        from uuid import UUID as _UUID
        ids = [_UUID(oid) for oid in order_ids]

        async with get_db_connection() as conn:
            timezone_name = await resolve_tenant_timezone(conn, tenant_id)
            # Guard: fail fast if any order falls in a closed monthly accounting period (#362)
            closed_check = await conn.fetchrow(
                """
                SELECT 1 FROM orders o
                JOIN tenant_monthly_periods mp
                    ON mp.tenant_id = o.tenant_id
                    AND EXTRACT(YEAR  FROM o.order_date AT TIME ZONE $3) = mp.year
                    AND EXTRACT(MONTH FROM o.order_date AT TIME ZONE $3) = mp.month
                    AND mp.status = 'closed'
                WHERE o.id = ANY($1) AND o.tenant_id = $2
                LIMIT 1
                """,
                ids, tenant_id, timezone_name,
            )
            if closed_check:
                raise APIError(
                    "Una o más órdenes pertenecen a un período contable cerrado. "
                    "Contacta a tu contador para realizar correcciones.",
                    status_code=409,
                )

            # Fetch current state of all orders before updating
            order_rows = await conn.fetch(
                """SELECT id, status, order_number, table_session_id, pos_cart_id,
                          payment_status, total_amount
                   FROM orders WHERE id = ANY($1) AND tenant_id = $2""",
                ids, tenant_id
            )

            # Block completed → pending for POS orders in bulk
            if status == 'pending':
                blocked = [str(r['id']) for r in order_rows if r['status'] == 'completed' and r['pos_cart_id']]
                if blocked:
                    raise APIError(
                        "Las órdenes completadas del POS no pueden volver a pendiente. Use 'Cancelar' en su lugar.",
                        status_code=400
                    )

            from uuid import UUID as _UUID2
            cid = _UUID2(customer_id) if customer_id else None
            pmid = _UUID2(payment_method_id) if payment_method_id else None

            if payment_method_id and not payment_method:
                raise APIError("payment_method es requerido cuando se envía payment_method_id", status_code=400)

            if payment_method:
                group_row = await conn.fetchrow(
                    """
                    SELECT id
                    FROM payment_method_groups
                    WHERE (tenant_id = $1 OR tenant_id IS NULL)
                      AND slug = $2
                    """,
                    tenant_id,
                    payment_method,
                )
                if payment_method_id:
                    if not group_row:
                        raise APIError(
                            f"Método de pago '{payment_method}' no es válido para este restaurante.",
                            status_code=400,
                        )
                    method_row = await conn.fetchrow(
                        """
                        SELECT id
                        FROM payment_methods
                        WHERE id = $1
                          AND tenant_id = $2
                          AND group_id = $3
                          AND is_active = true
                        """,
                        pmid,
                        tenant_id,
                        group_row["id"],
                    )
                    if not method_row:
                        raise APIError("El método seleccionado no pertenece al grupo elegido.", status_code=400)

                if payment_method == "customer_wallet" and not cid:
                    raise APIError("La billetera requiere un cliente identificado", status_code=400)

                if payment_method == "customer_wallet" and cid:
                    from app.services.customer_wallet_service import assert_wallet_customer_identified

                    await assert_wallet_customer_identified(conn, cid)

            payment_status_update = None
            if status == "completed" and payment_method:
                payment_status_update = "credit" if payment_method == "credit" else "paid"

            result = await conn.execute(
                """UPDATE orders
                   SET status = $1,
                       payment_method = COALESCE($2, payment_method),
                       customer_id = COALESCE($5, customer_id),
                       payment_method_id = CASE WHEN $2::text IS NULL THEN payment_method_id ELSE $6::uuid END,
                       payment_status = COALESCE($7, payment_status)
                   WHERE id = ANY($3) AND tenant_id = $4""",
                status, payment_method, ids, tenant_id, cid, pmid, payment_status_update
            )

            if status == "completed" and payment_method == "customer_wallet" and cid:
                from app.services.customer_wallet_service import apply_wallet_for_order

                for row in order_rows:
                    if row["status"] == "completed":
                        continue
                    amount_cop = Decimal(str(row["total_amount"]))
                    if amount_cop > 0:
                        await apply_wallet_for_order(
                            conn,
                            cid,
                            tenant_id,
                            amount_cop,
                            row["id"],
                            user_id,
                        )

            # Stock adjustments and mesa session releases per order
            released_sessions = set()
            newly_completed_order_ids = []
            for row in order_rows:
                old_status = row['status']
                if old_status == status:
                    continue

                order_id_row = row['id']
                order_number = int(row['order_number'])

                # Stock
                if old_status != 'completed' and status == 'completed':
                    if not (row['pos_cart_id'] and old_status == 'pending'):
                        await _deduct_stock_for_status_update(conn, order_id_row, tenant_id, user_id, order_number)
                    newly_completed_order_ids.append(order_id_row)
                elif old_status == 'completed' and status in ('cancelled', 'pending'):
                    await _return_stock_for_order_cancellation(conn, order_id_row, tenant_id, user_id, order_number)

                # If cancelling a credit order, clear payment_status so it leaves cartera
                if status == 'cancelled' and row['payment_status'] in ('credit', 'partial'):
                    await conn.execute(
                        "UPDATE orders SET payment_status = NULL WHERE id = $1",
                        order_id_row
                    )

                # Mesa session (deduplicated by session id)
                if status in ('completed', 'cancelled') and row['table_session_id']:
                    sid = row['table_session_id']
                    if sid not in released_sessions:
                        released_sessions.add(sid)
                        await conn.execute(
                            "UPDATE table_sessions SET closed_at = now() WHERE id = $1 AND closed_at IS NULL",
                            sid
                        )
                        await conn.execute(
                            """UPDATE tables SET status = 'free'
                               WHERE id = (SELECT table_id FROM table_sessions WHERE id = $1)
                                 AND tenant_id = $2""",
                            sid, tenant_id
                        )

            if newly_completed_order_ids:
                try:
                    completed_orders = await conn.fetch(
                        """
                        SELECT id, order_number, total_amount, payment_method,
                               payment_method_id, order_date
                        FROM orders
                        WHERE id = ANY($1) AND tenant_id = $2 AND status = 'completed'
                        """,
                        newly_completed_order_ids,
                        tenant_id,
                    )
                    tax_config = await _get_tenant_tax_config(conn, tenant_id)
                    for ord_row in completed_orders:
                        gl_order_date = local_date_for_tenant(ord_row["order_date"], timezone_name)
                        await _post_order_gl_entry(
                            conn=conn,
                            tenant_id=tenant_id,
                            order_id=ord_row["id"],
                            order_date=gl_order_date,
                            total_amount=Decimal(str(ord_row["total_amount"])),
                            payment_method=ord_row["payment_method"] or payment_method or "digital",
                            payment_method_id=ord_row["payment_method_id"],
                            tax_config=tax_config,
                            order_number=int(ord_row["order_number"]),
                        )
                        await _post_order_cogs_gl_entry(
                            conn=conn,
                            tenant_id=tenant_id,
                            order_id=ord_row["id"],
                            order_date=gl_order_date,
                            order_number=int(ord_row["order_number"]),
                        )
                except MissingAccountRoleError:
                    raise
                except Exception as gl_exc:
                    logger.error(f"GL entries failed for bulk status update: {gl_exc}")

        updated = int(result.split()[-1])
        return {"success": True, "updated": updated, "message": f"{updated} orden(es) actualizadas a {status}"}

    except (AuthenticationError, APIError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error bulk updating order status: {str(e)}")
        raise APIError(f"Error al actualizar órdenes: {str(e)}", status_code=500)


async def update_order_status(
    request: Request,
    order_id: UUID,
    status: str,
    payment_method: Optional[str] = None,
    payment_method_id: Optional[str] = None,
    customer_id: Optional[str] = None,
) -> dict:
    """Update the status of an order (mesa orders only)."""
    allowed = {"completed", "cancelled", "pending", "preparing"}
    if status not in allowed:
        raise APIError(f"Estado inválido. Valores permitidos: {', '.join(allowed)}", status_code=400)

    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        waros_award_order_id = None
        waros_award_customer_id = None

        async with get_db_connection() as conn:
            timezone_name = await resolve_tenant_timezone(conn, tenant_id)
            row = await conn.fetchrow(
                """SELECT id, status, order_number, table_session_id, pos_cart_id,
                          payment_status, order_date, total_amount, customer_id
                   FROM orders WHERE id = $1 AND tenant_id = $2""",
                order_id, tenant_id
            )
            if not row:
                raise APIError("Orden no encontrada", status_code=404)

            # Guard: block mutation if order falls in a closed monthly accounting period (#362)
            await assert_order_not_in_closed_monthly_period(conn, tenant_id, row['order_date'])

            old_status = row['status']
            order_number = int(row['order_number'])

            # Block completed → pending for POS orders (no active table session to restore)
            if old_status == 'completed' and status == 'pending' and row['pos_cart_id']:
                raise APIError(
                    "Las órdenes completadas del POS no pueden volver a pendiente. Use 'Cancelar' en su lugar.",
                    status_code=400
                )

            try:
                pmid = UUID(payment_method_id) if payment_method_id else None
            except ValueError:
                raise APIError(
                    "payment_method_id no es un UUID válido",
                    status_code=400,
                    details={"code": "payment_method_id_invalid"},
                )
            cid = UUID(customer_id) if customer_id else row["customer_id"]

            if payment_method_id and not payment_method:
                raise APIError(
                    "payment_method es requerido cuando se envía payment_method_id",
                    status_code=400,
                    details={"code": "payment_method_required"},
                )

            if status == "completed" and not payment_method:
                raise APIError(
                    "Selecciona un método de pago para completar la orden",
                    status_code=400,
                    details={"code": "payment_method_required"},
                )

            if payment_method:
                group_row = await conn.fetchrow(
                    """
                    SELECT id
                    FROM payment_method_groups
                    WHERE (tenant_id = $1 OR tenant_id IS NULL)
                      AND slug = $2
                    """,
                    tenant_id,
                    payment_method,
                )
                if payment_method_id:
                    if not group_row:
                        raise APIError(
                            f"Método de pago '{payment_method}' no es válido para este restaurante.",
                            status_code=400,
                            details={"code": "payment_method_invalid"},
                        )
                    method_row = await conn.fetchrow(
                        """
                        SELECT id
                        FROM payment_methods
                        WHERE id = $1
                          AND tenant_id = $2
                          AND group_id = $3
                          AND is_active = true
                        """,
                        pmid,
                        tenant_id,
                        group_row["id"],
                    )
                    if not method_row:
                        raise APIError(
                            "El método seleccionado no pertenece al grupo elegido.",
                            status_code=400,
                            details={"code": "payment_method_id_invalid"},
                        )

                if payment_method == "customer_wallet" and not cid:
                    raise APIError(
                        "La billetera requiere un cliente identificado",
                        status_code=400,
                        details={"code": "customer_required"},
                    )

                if payment_method == "customer_wallet" and cid:
                    from app.services.customer_wallet_service import assert_wallet_customer_identified

                    await assert_wallet_customer_identified(conn, cid)

            payment_status_update = None
            if status == "completed" and payment_method:
                payment_status_update = "credit" if payment_method == "credit" else "paid"

            await conn.execute(
                """UPDATE orders
                   SET status = $1,
                       payment_method = COALESCE($2, payment_method),
                       payment_method_id = CASE WHEN $2::text IS NULL THEN payment_method_id ELSE $4::uuid END,
                       customer_id = COALESCE($5, customer_id),
                       payment_status = COALESCE($6, payment_status)
                   WHERE id = $3""",
                status, payment_method, order_id, pmid, cid, payment_status_update
            )

            if status == "completed" and payment_method == "customer_wallet" and cid:
                from app.services.customer_wallet_service import apply_wallet_for_order

                if old_status != "completed":
                    amount_cop = Decimal(str(row["total_amount"]))
                    if amount_cop > 0:
                        await apply_wallet_for_order(
                            conn,
                            cid,
                            tenant_id,
                            amount_cop,
                            order_id,
                            user_id,
                        )

            # If cancelling a credit order, clear payment_status so it leaves cartera
            if status == 'cancelled' and row['payment_status'] in ('credit', 'partial'):
                await conn.execute(
                    "UPDATE orders SET payment_status = NULL WHERE id = $1",
                    order_id
                )

            # Stock adjustment based on transition
            if old_status != status:
                if old_status != 'completed' and status == 'completed':
                    inventory_already_consumed = await _order_inventory_already_consumed_before_completion(
                        conn,
                        row=row,
                        order_id=order_id,
                        tenant_id=tenant_id,
                        old_status=old_status,
                    )
                    if not inventory_already_consumed:
                        await _deduct_stock_for_status_update(conn, order_id, tenant_id, user_id, order_number)
                    try:
                        completed_order = await conn.fetchrow(
                            """
                            SELECT id, order_number, total_amount, payment_method,
                                   payment_method_id, order_date, tip_amount, tip_tax_amount
                            FROM orders
                            WHERE id = $1 AND tenant_id = $2 AND status = 'completed'
                            """,
                            order_id,
                            tenant_id,
                        )
                        if completed_order:
                            if cid:
                                waros_award_order_id = completed_order["id"]
                                waros_award_customer_id = cid
                            gl_order_date = local_date_for_tenant(completed_order["order_date"], timezone_name)
                            tax_config = await _get_tenant_tax_config(conn, tenant_id)
                            await _post_order_gl_entry(
                                conn=conn,
                                tenant_id=tenant_id,
                                order_id=completed_order["id"],
                                order_date=gl_order_date,
                                total_amount=Decimal(str(completed_order["total_amount"])),
                                payment_method=completed_order["payment_method"] or payment_method,
                                payment_method_id=completed_order["payment_method_id"],
                                tax_config=tax_config,
                                order_number=int(completed_order["order_number"]),
                                tip_amount=Decimal(str(completed_order["tip_amount"] or 0)),
                                tip_tax_amount=Decimal(str(completed_order["tip_tax_amount"] or 0)),
                            )
                            await _post_order_cogs_gl_entry(
                                conn=conn,
                                tenant_id=tenant_id,
                                order_id=completed_order["id"],
                                order_date=gl_order_date,
                                order_number=int(completed_order["order_number"]),
                            )
                    except MissingAccountRoleError:
                        raise
                    except Exception as gl_exc:
                        logger.error(f"GL entries failed for order status update: {gl_exc}")
                elif old_status == 'completed' and status in ('cancelled', 'pending'):
                    await _return_stock_for_order_cancellation(conn, order_id, tenant_id, user_id, order_number)

            # Release the table session if this is a mesa order being closed
            if status in ("completed", "cancelled") and row['table_session_id']:
                await conn.execute(
                    "UPDATE table_sessions SET closed_at = now() WHERE id = $1 AND closed_at IS NULL",
                    row['table_session_id']
                )
                await conn.execute(
                    """UPDATE tables SET status = 'free'
                       WHERE id = (SELECT table_id FROM table_sessions WHERE id = $1)
                         AND tenant_id = $2""",
                    row['table_session_id'], tenant_id
                )

            # Auto-fire hook for manual orders transitioning to 'preparing'
            if status == "preparing":
                try:
                    # Check if comandas are enabled
                    prof = await conn.fetchrow(
                        "SELECT comandas_enabled FROM tenant_public_profiles WHERE tenant_id = $1",
                        tenant_id
                    )
                    if prof and prof["comandas_enabled"]:
                        # Manual orders are treated as 'delivery' (generic out-of-table order)
                        from app.services.comandas_service import fire_comandas
                        await fire_comandas(
                            order_id=order_id,
                            tenant_id=tenant_id,
                            source_type='delivery',
                            table_display_name='Domicilio',
                            conn=conn
                        )
                except Exception as _fe:
                    logger.error(f"Auto-fire failed for manual order {order_id} (preparing): {_fe}")

        if waros_award_order_id and waros_award_customer_id:
            try:
                asyncio.create_task(
                    evaluate_and_award(waros_award_order_id, waros_award_customer_id, tenant_id)
                )
            except Exception as _waros_err:
                logger.warning(f"Could not schedule waros evaluation: {_waros_err}")

        return {"success": True, "message": f"Estado actualizado a {status}"}

    except (AuthenticationError, APIError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error updating order status: {str(e)}")
        raise APIError(f"Error al actualizar estado: {str(e)}", status_code=500)


async def associate_order_customer(
    request: Request,
    order_id: UUID,
    customer_id: UUID,
) -> dict:
    """Associate an order with a tenant customer before electronic invoicing."""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            order_row = await conn.fetchrow(
                """
                SELECT id
                FROM orders
                WHERE id = $1
                  AND tenant_id = $2
                  AND (pos_cart_id IS NOT NULL OR table_session_id IS NOT NULL OR extra_attributes->>'source' = 'manual')
                """,
                order_id,
                tenant_id,
            )
            if not order_row:
                raise APIError("Order not found", status_code=404)

            invoice_row = await conn.fetchrow(
                """
                SELECT id
                FROM electronic_invoices
                WHERE order_id = $1
                  AND tenant_id = $2
                LIMIT 1
                """,
                order_id,
                tenant_id,
            )
            if invoice_row:
                raise APIError(
                    "No se puede cambiar el cliente porque la venta ya tiene factura electrónica.",
                    status_code=409,
                    details={"code": "invoice_exists"},
                )

            customer_row = await conn.fetchrow(
                """
                SELECT p.id
                FROM profile p
                JOIN tenant_customers tc ON tc.profile_id = p.id
                WHERE p.id = $1
                  AND tc.tenant_id = $2
                  AND tc.is_active = true
                LIMIT 1
                """,
                customer_id,
                tenant_id,
            )
            if not customer_row:
                raise APIError("Customer not found", status_code=404)

            await conn.execute(
                """
                UPDATE orders
                SET customer_id = $1
                WHERE id = $2
                  AND tenant_id = $3
                """,
                customer_id,
                order_id,
                tenant_id,
            )

        return {
            "success": True,
            "message": "Cliente asociado a la venta",
            "customer_id": str(customer_id),
        }

    except (AuthenticationError, APIError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error associating order customer: {str(e)}")
        raise APIError(f"Error al asociar cliente: {str(e)}", status_code=500)


async def _order_inventory_already_consumed_before_completion(
    conn,
    *,
    row,
    order_id: UUID,
    tenant_id: UUID,
    old_status: str,
) -> bool:
    """Return true when a pending order already consumed inventory at creation time."""
    if old_status != "pending":
        return False
    if row["pos_cart_id"]:
        return True
    if not row["table_session_id"]:
        return False

    return bool(await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1
            FROM tenant_ingredient_movements
            WHERE tenant_id = $1
              AND reference_table = 'orders'
              AND reference_id = $2
              AND movement_type = 'consumption'
              AND quantity_change < 0
        )
        """,
        tenant_id,
        order_id,
    ))


async def get_order_items(
    request: Request,
    order_id: UUID
) -> dict:
    """
    Get order items with modifiers for a specific order
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            # First verify order exists and belongs to this tenant
            verify_query = """
                SELECT id FROM orders
                WHERE id = $1 AND tenant_id = $2
                  AND (pos_cart_id IS NOT NULL OR table_session_id IS NOT NULL OR extra_attributes->>'source' = 'manual')
            """
            order_exists = await conn.fetchrow(verify_query, order_id, tenant_id)

            if not order_exists:
                raise APIError("Order not found", status_code=404)

            # Get order items with product details
            items_query = """
                SELECT
                    oi.id,
                    oi.quantity,
                    oi.price_at_purchase,
                    oi.subtotal,
                    oi.discount_allocated,
                    oi.net_total,
                    oi.applied_promotion_id,
                    oi.promo_savings_allocated,
                    tp.name AS promotion_name,
                    tp.promo_type AS promotion_type,
                    p.id as product_id,
                    p.name as product_name,
                    p.description as product_description
                FROM order_items oi
                LEFT JOIN product p ON oi.product_id = p.id
                LEFT JOIN tenant_promotions tp ON tp.id = oi.applied_promotion_id
                WHERE oi.order_id = $1
                ORDER BY oi.created_at
            """
            items_rows = await conn.fetch(items_query, order_id)

            items = []
            for item_row in items_rows:
                # Get modifiers for this item
                modifiers_query = """
                    SELECT
                        id,
                        modifier_id,
                        modifier_name,
                        price_at_purchase,
                        quantity,
                        included_quantity_at_purchase
                    FROM order_item_modifiers
                    WHERE order_item_id = $1
                """
                modifiers_rows = await conn.fetch(modifiers_query, item_row['id'])

                modifiers = [
                    {
                        "id": str(mod['id']),  # order_item_modifier ID for deletion
                        "modifier_id": str(mod['modifier_id']) if mod['modifier_id'] else None,
                        "name": mod['modifier_name'],
                        "price": float(mod['price_at_purchase']),
                        "quantity": int(mod['quantity'] or 1),
                        "included_quantity": int(
                            mod["included_quantity_at_purchase"] or 0
                        ),
                    }
                    for mod in modifiers_rows
                ]

                items.append({
                    "id": str(item_row['id']),
                    "quantity": item_row['quantity'],
                    "price_at_purchase": float(item_row['price_at_purchase']),
                    "subtotal": float(item_row['subtotal']),
                    "discount_allocated": float(item_row['discount_allocated']) if item_row['discount_allocated'] is not None else 0.0,
                    "net_total": float(item_row['net_total']) if item_row['net_total'] is not None else None,
                    "applied_promotion_id": str(item_row['applied_promotion_id']) if item_row['applied_promotion_id'] else None,
                    "promo_savings_allocated": float(item_row['promo_savings_allocated']) if item_row['promo_savings_allocated'] is not None else 0.0,
                    "promotion_name": item_row['promotion_name'],
                    "promotion_type": item_row['promotion_type'],
                    "product": {
                        "id": str(item_row['product_id']) if item_row['product_id'] else None,
                        "name": item_row['product_name'],
                        "description": item_row['product_description'],
                        "image_url": None
                    },
                    "modifiers": modifiers
                })

            return {
                "success": True,
                "data": items
            }

    except (AuthenticationError, APIError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting order items: {str(e)}")
        raise APIError(f"Error getting order items: {str(e)}", status_code=500)


async def get_customers_list(
    request: Request,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    payment_method: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 100,
    offset: int = 0
) -> dict:
    """
    List tenant customers with sales order metrics for the filtered period.
    Includes profiles linked via tenant_members even when they have zero orders
    (warocol.com#1099). Payment/status filters require a matching order.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            timezone_name = await resolve_tenant_timezone(conn, tenant_id)
            order_conditions = [
                "o.tenant_id = $1",
                ANALYTICS_SALES_FILTER_ALIAS_O,
                "o.customer_id IS NOT NULL",
            ]
            params: list = [tenant_id]
            param_count = 1

            if payment_method:
                param_count += 1
                order_conditions.append(f"o.payment_method = ${param_count}")
                params.append(payment_method)

            if status:
                param_count += 1
                order_conditions.append(f"o.status = ${param_count}")
                params.append(status)

            parsed_date_from = parse_date(date_from)
            parsed_date_to = parse_date(date_to)
            param_count = _append_local_date_bounds(
                order_conditions,
                params,
                param_count,
                "o.order_date",
                parsed_date_from,
                parsed_date_to,
                timezone_name,
            )

            order_where = " AND ".join(order_conditions)

            outer_conditions: list[str] = []
            if search:
                param_count += 1
                outer_conditions.append(
                    f"(tcp.name ILIKE ${param_count} OR tcp.phone ILIKE ${param_count})"
                )
                params.append(f"%{search}%")

            outer_where = ""
            if outer_conditions:
                outer_where = "WHERE " + " AND ".join(outer_conditions)

            require_order_match = bool(payment_method or status)
            join_type = "INNER" if require_order_match else "LEFT"

            param_count += 1
            timezone_param = param_count
            params.append(timezone_name)

            param_count += 1
            limit_param = param_count
            param_count += 1
            offset_param = param_count

            query = f"""
                WITH tenant_customer_profiles AS (
                    SELECT
                        p.id AS customer_id,
                        COALESCE(p.name, 'Sin identificar') AS name,
                        p.phone_number AS phone
                    FROM profile p
                    INNER JOIN tenant_customers tc ON tc.profile_id = p.id
                    WHERE tc.tenant_id = $1
                      AND tc.is_active = true
                ),
                order_agg AS (
                    SELECT
                        o.customer_id,
                        SUM(o.total_amount) AS total_spent,
                        COUNT(o.id)         AS order_count,
                        AVG(o.total_amount) AS avg_ticket,
                        MAX(DATE(o.order_date AT TIME ZONE ${timezone_param})) AS last_order_date
                    FROM orders o
                    WHERE {order_where}
                    GROUP BY o.customer_id
                )
                SELECT
                    tcp.customer_id,
                    tcp.name,
                    tcp.phone,
                    COALESCE(oa.total_spent, 0)   AS total_spent,
                    COALESCE(oa.order_count, 0)   AS order_count,
                    COALESCE(oa.avg_ticket, 0)    AS avg_ticket,
                    oa.last_order_date,
                    COUNT(*) OVER() AS total_count,
                    SUM(COALESCE(oa.total_spent, 0)) OVER() AS total_revenue
                FROM tenant_customer_profiles tcp
                {join_type} JOIN order_agg oa ON oa.customer_id = tcp.customer_id
                {outer_where}
                ORDER BY COALESCE(oa.total_spent, 0) DESC, tcp.name ASC
                LIMIT ${limit_param} OFFSET ${offset_param}
            """

            params.extend([limit, offset])
            rows = await conn.fetch(query, *params)
            total_count = rows[0]['total_count'] if rows else 0
            total_revenue = float(rows[0]['total_revenue']) if rows else 0.0

            customers = [
                {
                    "customer_id": str(row['customer_id']),
                    "name": row['name'],
                    "phone": row['phone'],
                    "total_spent": float(row['total_spent']),
                    "order_count": int(row['order_count']),
                    "avg_ticket": float(row['avg_ticket'] or 0),
                    "last_order_date": (
                        row['last_order_date'].isoformat()
                        if row['last_order_date']
                        else None
                    ),
                }
                for row in rows
            ]

            return {
                "success": True,
                "data": customers,
                "total": total_count,
                "total_revenue": total_revenue,
                "limit": limit,
                "offset": offset,
            }

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting customers list: {str(e)}")
        raise APIError(f"Error getting customers list: {str(e)}", status_code=500)


async def get_customer_detail(
    request: Request,
    customer_id: UUID,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page: int = 1,
    per_page: int = 20
) -> dict:
    """
    Get a single customer's aggregate stats plus their paginated sales order history.
    Returns 404 if the customer is not linked to the tenant.
    Profiles without sales orders return zeroed stats and empty order history.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            timezone_name = await resolve_tenant_timezone(conn, tenant_id)
            # --- Customer aggregate stats ---
            parsed_date_from = parse_date(date_from)
            parsed_date_to = parse_date(date_to)

            aggregate_conditions = [
                "o.tenant_id = $1",
                ANALYTICS_SALES_FILTER_ALIAS_O,
                "o.customer_id = $2",
            ]
            aggregate_params = [tenant_id, customer_id, timezone_name]
            aggregate_param_count = 3
            aggregate_param_count = _append_local_date_bounds(
                aggregate_conditions,
                aggregate_params,
                aggregate_param_count,
                "o.order_date",
                parsed_date_from,
                parsed_date_to,
                timezone_name,
            )
            aggregate_where_clause = " AND ".join(aggregate_conditions)

            customer_row = await conn.fetchrow(
                f"""
                SELECT
                    o.customer_id,
                    COALESCE(p.name, 'Sin identificar') AS name,
                    p.phone_number                       AS phone,
                    p.email                              AS email,
                    COUNT(o.id)                          AS total_orders,
                    SUM(o.total_amount)                  AS total_spent,
                    MIN(DATE(o.order_date AT TIME ZONE $3)) AS first_purchase,
                    MAX(DATE(o.order_date AT TIME ZONE $3)) AS last_purchase
                FROM orders o
                LEFT JOIN profile p ON o.customer_id = p.id
                WHERE {aggregate_where_clause}
                GROUP BY o.customer_id, p.name, p.phone_number, p.email
                """,
                *aggregate_params,
            )

            if not customer_row:
                profile_row = await conn.fetchrow(
                    """
                    SELECT
                        p.id                                 AS customer_id,
                        COALESCE(p.name, 'Sin identificar') AS name,
                        p.phone_number                       AS phone,
                        p.email                              AS email
                    FROM profile p
                    JOIN tenant_customers tc ON tc.profile_id = p.id
                    WHERE p.id = $1
                      AND tc.tenant_id = $2
                      AND tc.is_active = true
                    LIMIT 1
                    """,
                    customer_id,
                    tenant_id,
                )
                if not profile_row:
                    raise APIError("Customer not found", status_code=404)
                customer_row = {
                    "customer_id": profile_row["customer_id"],
                    "name": profile_row["name"],
                    "phone": profile_row["phone"],
                    "email": profile_row["email"],
                    "total_orders": 0,
                    "total_spent": 0,
                    "first_purchase": None,
                    "last_purchase": None,
                }

            # --- Paginated order history ---
            where_conditions = [
                "o.tenant_id = $1",
                ANALYTICS_SALES_FILTER_ALIAS_O,
                "o.customer_id = $2",
            ]
            params = [tenant_id, customer_id]
            param_count = 2

            param_count = _append_local_date_bounds(
                where_conditions,
                params,
                param_count,
                "o.order_date",
                parsed_date_from,
                parsed_date_to,
                timezone_name,
            )

            where_clause = " AND ".join(where_conditions)

            offset = (page - 1) * per_page
            param_count += 1
            limit_param = param_count
            param_count += 1
            offset_param = param_count

            orders_query = f"""
                SELECT
                    o.id                AS order_id,
                    o.order_number,
                    o.order_date,
                    o.total_amount,
                    o.status,
                    o.payment_method,
                    (
                        SELECT COUNT(*)
                        FROM order_items oi
                        WHERE oi.order_id = o.id
                    )                   AS items_count,
                    (
                        SELECT COALESCE(SUM(wt.waros_amount), 0)
                        FROM waros_transactions wt
                        WHERE wt.related_entity_id = o.id::text
                          AND wt.transaction_type = 'earned'
                          AND wt.tenant_id = $1
                    )                   AS waros_earned,
                    ei.id               AS invoice_id,
                    ei.prefix           AS invoice_prefix,
                    ei.invoice_number   AS invoice_number,
                    ei.status           AS invoice_status,
                    ei.cufe             AS invoice_cufe,
                    COUNT(*) OVER()     AS total_count
                FROM orders o
                LEFT JOIN LATERAL (
                    SELECT id, prefix, invoice_number, status, cufe
                    FROM electronic_invoices
                    WHERE order_id = o.id AND tenant_id = $1
                    ORDER BY created_at DESC LIMIT 1
                ) ei ON true
                WHERE {where_clause}
                ORDER BY o.order_date DESC
                LIMIT ${limit_param} OFFSET ${offset_param}
            """

            params.extend([per_page, offset])
            order_rows = await conn.fetch(orders_query, *params)
            total_orders_in_period = order_rows[0]['total_count'] if order_rows else 0

            orders = [
                {
                    "order_id": str(row['order_id']),
                    "order_number": int(row['order_number']),
                    "date": row['order_date'].isoformat(),
                    "total": float(row['total_amount']),
                    "items_count": int(row['items_count']),
                    "payment_method": row['payment_method'],
                    "status": row['status'],
                    "waros_earned": int(row['waros_earned']),
                    "invoice_id": str(row['invoice_id']) if row['invoice_id'] else None,
                    "invoice_prefix": row['invoice_prefix'],
                    "invoice_number": row['invoice_number'],
                    "invoice_status": row['invoice_status'],
                    "invoice_cufe": row['invoice_cufe'],
                }
                for row in order_rows
            ]

            # --- Waros summary (same connection, no extra round-trip) ---
            wallet_row = await conn.fetchrow(
                """
                SELECT current_balance, lifetime_earned, lifetime_spent
                FROM waros_wallets
                WHERE profile_id = $1 AND tenant_id = $2
                """,
                customer_id,
                tenant_id,
            )

            manual_tx_rows = await conn.fetch(
                """
                SELECT id, created_at, waros_amount, description
                FROM waros_transactions
                WHERE profile_id = $1 AND tenant_id = $2
                  AND transaction_type = 'manual'
                ORDER BY created_at DESC
                """,
                customer_id,
                tenant_id,
            )

            waros_summary = {
                "current_balance": int(wallet_row["current_balance"]) if wallet_row else 0,
                "lifetime_earned": int(wallet_row["lifetime_earned"]) if wallet_row else 0,
                "lifetime_spent": int(wallet_row["lifetime_spent"]) if wallet_row else 0,
                "manual_transactions": [
                    {
                        "id": row["id"],
                        "created_at": row["created_at"].isoformat(),
                        "waros_amount": row["waros_amount"],
                        "description": row["description"],
                    }
                    for row in manual_tx_rows
                ],
            }

            return {
                "success": True,
                "customer": {
                    "customer_id": str(customer_row['customer_id']),
                    "name": customer_row['name'],
                    "phone": customer_row['phone'],
                    "email": customer_row['email'],
                    "total_orders": int(customer_row['total_orders']),
                    "total_spent": float(customer_row['total_spent']),
                    "first_purchase": _date_iso(customer_row['first_purchase']),
                    "last_purchase": _date_iso(customer_row['last_purchase']),
                },
                "orders": {
                    "items": orders,
                    "total": total_orders_in_period,
                    "page": page,
                    "per_page": per_page,
                },
                "waros_summary": waros_summary,
            }

    except (AuthenticationError, APIError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting customer detail {customer_id}: {str(e)}")
        raise APIError(f"Error getting customer detail: {str(e)}", status_code=500)


async def get_orders_metrics(
    request: Request,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    payment_method: Optional[str] = None,
    payment_method_id: Optional[str] = None,
    status: Optional[str] = None
) -> dict:
    """
    Get sales metrics: total sales, average ticket, orders count by status
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            timezone_name = await resolve_tenant_timezone(conn, tenant_id)
            # Build WHERE clause
            where_conditions = ["tenant_id = $1", ANALYTICS_SALES_FILTER]
            params = [tenant_id]
            param_count = 1

            parsed_date_from = parse_date(date_from)
            parsed_date_to = parse_date(date_to)
            param_count = _append_local_date_bounds(
                where_conditions,
                params,
                param_count,
                "order_date",
                parsed_date_from,
                parsed_date_to,
                timezone_name,
            )

            if payment_method:
                param_count += 1
                where_conditions.append(f"payment_method = ${param_count}")
                params.append(payment_method)

            if payment_method_id:
                param_count += 1
                where_conditions.append(f"payment_method_id = ${param_count}::uuid")
                params.append(payment_method_id)

            if status:
                param_count += 1
                where_conditions.append(f"status = ${param_count}")
                params.append(status)

            where_clause = " AND ".join(where_conditions)

            metrics_query = f"""
                SELECT
                    COUNT(*) as total_orders,
                    COUNT(*) FILTER (WHERE status = 'completed') as completed_orders,
                    COUNT(*) FILTER (WHERE status = 'cancelled') as cancelled_orders,
                    COUNT(*) FILTER (WHERE status = 'pending') as pending_orders,
                    COALESCE(SUM(total_amount) FILTER (WHERE status = 'completed'), 0) as total_sales,
                    COALESCE(AVG(total_amount) FILTER (WHERE status = 'completed'), 0) as avg_ticket,
                    COUNT(*) FILTER (WHERE status = 'completed' AND discount_amount > 0) as discount_count,
                    COALESCE(SUM(discount_amount) FILTER (WHERE status = 'completed' AND discount_amount > 0), 0) as total_discount_amount
                FROM orders
                WHERE {where_clause}
            """

            row = await conn.fetchrow(metrics_query, *params)

            # Tax aggregate — sum subtotals by tax_category for completed orders in same date range
            _total_std_tax = 0.0
            _total_liq_tax = 0.0
            _tax_label = "Impuesto"
            try:
                tax_config = await _get_tenant_tax_config(conn, tenant_id)
                # Rebuild WHERE without payment_method_id for tax query (order_items has no that filter)
                tax_where = ["o.tenant_id = $1", "o.status = 'completed'",
                             ANALYTICS_SALES_FILTER_ALIAS_O]
                tax_params: List[Any] = [tenant_id]
                tax_pc = 1
                tax_pc = _append_local_date_bounds(
                    tax_where,
                    tax_params,
                    tax_pc,
                    "o.order_date",
                    parsed_date_from,
                    parsed_date_to,
                    timezone_name,
                )
                if payment_method:
                    tax_pc += 1
                    tax_where.append(f"o.payment_method = ${tax_pc}")
                    tax_params.append(payment_method)
                if payment_method_id:
                    tax_pc += 1
                    tax_where.append(f"o.payment_method_id = ${tax_pc}::uuid")
                    tax_params.append(payment_method_id)
                tax_where_sql = " AND ".join(tax_where)
                tax_rows = await conn.fetch(
                    f"""SELECT COALESCE(p.tax_category, 'standard') AS tax_category,
                               COALESCE(SUM(oi.subtotal), 0) AS subtotal
                        FROM order_items oi
                        JOIN product p ON p.id = oi.product_id
                        JOIN orders o ON o.id = oi.order_id
                        WHERE {tax_where_sql}
                        GROUP BY COALESCE(p.tax_category, 'standard')""",
                    *tax_params
                )
                _total_std_tax, _total_liq_tax, _tax_label = _compute_tax_breakdown(tax_rows, tax_config)
            except Exception as _e:
                logger.warning(f"Tax metrics computation failed: {_e}")

            return {
                "success": True,
                "data": {
                    "total_sales": float(row['total_sales']),
                    "total_orders": row['total_orders'],
                    "completed_orders": row['completed_orders'],
                    "cancelled_orders": row['cancelled_orders'],
                    "pending_orders": row['pending_orders'],
                    "avg_ticket": float(row['avg_ticket']),
                    "discount_count": int(row['discount_count']),
                    "total_discount_amount": float(row['total_discount_amount']),
                    "total_standard_tax": _total_std_tax,
                    "total_liquor_tax": _total_liq_tax,
                    "standard_tax_label": _tax_label,
                }
            }

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting orders metrics: {str(e)}")
        raise APIError(f"Error getting orders metrics: {str(e)}", status_code=500)


async def get_orders_dashboard(
    request: Request,
    payment_method: Optional[str] = None,
    status: Optional[str] = None
) -> dict:
    """
    Returns all metrics needed for the /ventas dashboard in a single DB query.
    Eliminates the need for 3 separate /orders/metrics calls on page load.

    Returns:
    - main: all-time metrics (filtered by payment_method/status if provided)
    - month: current month-to-date metrics (filtered by payment_method/status if provided)
    - year: current year-to-date metrics (filtered by payment_method/status if provided)
    - commission_savings: main.total_sales * commission_rate (from commission_configs or 30% default)
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            timezone_name = await resolve_tenant_timezone(conn, tenant_id)
            # Build optional filters for the main (all-time) metrics
            main_filters = []
            params = [tenant_id]
            param_count = 1

            if payment_method:
                param_count += 1
                main_filters.append(f"payment_method = ${param_count}")
                params.append(payment_method)

            if status:
                param_count += 1
                main_filters.append(f"status = ${param_count}")
                params.append(status)

            # Build FILTER clause suffix for main metrics (e.g. "AND payment_method = $2")
            main_filter_sql = ""
            if main_filters:
                main_filter_sql = " AND " + " AND ".join(main_filters)

            param_count += 1
            timezone_param = param_count
            params.append(timezone_name)

            dashboard_query = f"""
                SELECT
                    -- Main: all-time (with optional payment/status filters)
                    COUNT(*) FILTER (WHERE status = 'completed'{main_filter_sql}) as main_completed,
                    COALESCE(SUM(total_amount) FILTER (WHERE status = 'completed'{main_filter_sql}), 0) as main_sales,
                    COALESCE(AVG(total_amount) FILTER (WHERE status = 'completed'{main_filter_sql}), 0) as main_avg_ticket,
                    COUNT(*) FILTER (WHERE status = 'completed' AND discount_amount > 0{main_filter_sql}) as main_discount_count,
                    COALESCE(SUM(discount_amount) FILTER (WHERE status = 'completed' AND discount_amount > 0{main_filter_sql}), 0) as main_total_discount,

                    -- Month-to-date (with optional payment/status filters)
                    COUNT(*) FILTER (
                        WHERE status = 'completed'
                        AND DATE(order_date AT TIME ZONE ${timezone_param}) >= DATE_TRUNC('month', NOW() AT TIME ZONE ${timezone_param})::date
                        {main_filter_sql}
                    ) as month_completed,
                    COALESCE(SUM(total_amount) FILTER (
                        WHERE status = 'completed'
                        AND DATE(order_date AT TIME ZONE ${timezone_param}) >= DATE_TRUNC('month', NOW() AT TIME ZONE ${timezone_param})::date
                        {main_filter_sql}
                    ), 0) as month_sales,

                    -- Year-to-date (with optional payment/status filters)
                    COUNT(*) FILTER (
                        WHERE status = 'completed'
                        AND DATE(order_date AT TIME ZONE ${timezone_param}) >= DATE_TRUNC('year', NOW() AT TIME ZONE ${timezone_param})::date
                        {main_filter_sql}
                    ) as year_completed,
                    COALESCE(SUM(total_amount) FILTER (
                        WHERE status = 'completed'
                        AND DATE(order_date AT TIME ZONE ${timezone_param}) >= DATE_TRUNC('year', NOW() AT TIME ZONE ${timezone_param})::date
                        {main_filter_sql}
                    ), 0) as year_sales

                FROM orders
                WHERE tenant_id = $1 AND {ANALYTICS_SALES_FILTER}
            """

            row = await conn.fetchrow(dashboard_query, *params)

            # Fetch commission rate from tenant config (default 30% — typical Rappi/iFood Colombian rate)
            commission_row = await conn.fetchrow(
                "SELECT default_commission_percentage FROM commission_configs WHERE tenant_id = $1",
                tenant_id
            )
            commission_rate = float(commission_row['default_commission_percentage']) if commission_row else 30.0

            main_sales = float(row['main_sales'])
            commission_savings = round(main_sales * (commission_rate / 100))

            # Payment breakdown — UNION ALL: order_payments (split) + legacy orders
            breakdown_rows = await conn.fetch(
                f"""
                -- Split orders: amounts from order_payments
                SELECT
                    COALESCE(pmg.slug, op.payment_method)  AS group_slug,
                    COALESCE(pmg.name, op.payment_method)  AS group_name,
                    COALESCE(SUM(op.amount), 0)             AS total,
                    COUNT(DISTINCT op.order_id)             AS order_count
                FROM order_payments op
                JOIN orders o ON o.id = op.order_id
                LEFT JOIN payment_methods pm ON pm.id = op.payment_method_id
                LEFT JOIN payment_method_groups pmg ON pmg.id = pm.group_id
                WHERE o.tenant_id = $1
                  AND o.status = 'completed'
                  AND {ANALYTICS_SALES_FILTER_ALIAS_O}
                GROUP BY COALESCE(pmg.slug, op.payment_method), COALESCE(pmg.name, op.payment_method)

                UNION ALL

                -- Legacy orders (no rows in order_payments): use orders.total_amount
                SELECT
                    COALESCE(pmg.slug, o.payment_method)  AS group_slug,
                    COALESCE(pmg.name, o.payment_method)  AS group_name,
                    COALESCE(SUM(o.total_amount), 0)       AS total,
                    COUNT(*)                               AS order_count
                FROM orders o
                LEFT JOIN payment_methods pm ON pm.id = o.payment_method_id
                LEFT JOIN payment_method_groups pmg ON pmg.id = pm.group_id
                WHERE o.tenant_id = $1
                  AND o.status = 'completed'
                  AND {ANALYTICS_SALES_FILTER_ALIAS_O}
                  AND NOT EXISTS (SELECT 1 FROM order_payments op WHERE op.order_id = o.id)
                GROUP BY COALESCE(pmg.slug, o.payment_method), COALESCE(pmg.name, o.payment_method)
                """,
                tenant_id,
            )
            # Aggregate across UNION ALL branches in Python to avoid double-counting
            bd_agg: Dict[str, Any] = {}
            for r in breakdown_rows:
                slug = r["group_slug"]
                if slug is None:
                    continue
                if slug not in bd_agg:
                    bd_agg[slug] = {
                        "group_slug":  slug,
                        "group_name":  r["group_name"],
                        "total":       float(r["total"]),
                        "order_count": int(r["order_count"]),
                    }
                else:
                    entry = bd_agg[slug]
                    entry["total"] = float(entry["total"]) + float(r["total"])
                    entry["order_count"] = int(entry["order_count"]) + int(r["order_count"])
            payment_breakdown = sorted(bd_agg.values(), key=lambda x: x["total"], reverse=True)

            # Tax aggregates — all-time, month-to-date, year-to-date
            _main_std = 0.0
            _main_liq = 0.0
            _month_std = 0.0
            _month_liq = 0.0
            _year_std = 0.0
            _year_liq = 0.0
            _tax_label = "Impuesto"
            _base_filter = f"o.tenant_id = $1 AND o.status = 'completed' AND {ANALYTICS_SALES_FILTER_ALIAS_O}"
            _tax_select = """
                SELECT COALESCE(p.tax_category, 'standard') AS tax_category,
                       COALESCE(SUM(oi.subtotal), 0) AS subtotal
                FROM order_items oi
                JOIN product p ON p.id = oi.product_id
                JOIN orders o ON o.id = oi.order_id
            """
            try:
                tax_config = await _get_tenant_tax_config(conn, tenant_id)
                main_tax_rows = await conn.fetch(
                    f"{_tax_select} WHERE {_base_filter} GROUP BY COALESCE(p.tax_category, 'standard')",
                    tenant_id
                )
                month_tax_rows = await conn.fetch(
                    f"""{_tax_select} WHERE {_base_filter}
                        AND DATE(o.order_date AT TIME ZONE $2) >= DATE_TRUNC('month', NOW() AT TIME ZONE $2)::date
                        GROUP BY COALESCE(p.tax_category, 'standard')""",
                    tenant_id,
                    timezone_name,
                )
                year_tax_rows = await conn.fetch(
                    f"""{_tax_select} WHERE {_base_filter}
                        AND DATE(o.order_date AT TIME ZONE $2) >= DATE_TRUNC('year', NOW() AT TIME ZONE $2)::date
                        GROUP BY COALESCE(p.tax_category, 'standard')""",
                    tenant_id,
                    timezone_name,
                )
                _main_std, _main_liq, _tax_label = _compute_tax_breakdown(main_tax_rows, tax_config)
                _month_std, _month_liq, _ = _compute_tax_breakdown(month_tax_rows, tax_config)
                _year_std, _year_liq, _ = _compute_tax_breakdown(year_tax_rows, tax_config)
            except Exception as _e:
                logger.warning(f"Tax dashboard computation failed: {_e}")

            return {
                "success": True,
                "data": {
                    "main": {
                        "total_sales": main_sales,
                        "completed_orders": row['main_completed'],
                        "avg_ticket": float(row['main_avg_ticket']),
                        "discount_count": int(row['main_discount_count']),
                        "total_discount_amount": float(row['main_total_discount']),
                        "total_standard_tax": _main_std,
                        "total_liquor_tax": _main_liq,
                    },
                    "month": {
                        "total_sales": float(row['month_sales']),
                        "completed_orders": row['month_completed'],
                        "total_standard_tax": _month_std,
                        "total_liquor_tax": _month_liq,
                    },
                    "year": {
                        "total_sales": float(row['year_sales']),
                        "completed_orders": row['year_completed'],
                        "total_standard_tax": _year_std,
                        "total_liquor_tax": _year_liq,
                    },
                    "commission_savings": commission_savings,
                    "commission_rate": commission_rate,
                    "payment_breakdown": payment_breakdown,
                    "standard_tax_label": _tax_label,
                }
            }

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting orders dashboard: {str(e)}")
        raise APIError(f"Error getting orders dashboard: {str(e)}", status_code=500)


async def export_orders_to_email(
    request: Request,
    search: Optional[str] = None,
    search_field: Optional[str] = None,
    payment_method: Optional[str] = None,
    payment_method_id: Optional[str] = None,
    status: Optional[str] = None,
    sort_field: str = "order_date",
    sort_direction: str = "desc",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    tips_only: bool = False,
    member_id: Optional[str] = None,
    channel: Optional[str] = None,
) -> dict:
    """
    Export all orders (without pagination) based on filters and send via email.

    When `tips_only=True` (warocol.com#640), the result is restricted to
    orders with tip_amount > 0 and the CSV emits tip-specific columns
    (channel, mesero, propina, %). Additional filters `member_id` and
    `channel` apply only to this mode.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_email = session_context.email
        tenant_context = getattr(request.state, "tenant_context", None)
        tenant_name = getattr(tenant_context, "tenant_name", None) or "Waro Colombia"
        user_name = session_context.name or "Usuario"

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        if not user_email:
            raise APIError("No se encontró el correo del usuario", status_code=400)

        async with get_db_connection() as conn:
            locale_settings = await resolve_tenant_locale_settings(conn, tenant_id)
            locale = locale_settings.locale
            currency_code = locale_settings.currency_code
            timezone_name = locale_settings.timezone
            _ = get_translator(locale)
            # Build WHERE clause (same as get_orders_list but without pagination).
            # warocol.com#640 — tips-only mode swaps the orders-list base predicate
            # for tip_amount > 0 (all tips, regardless of channel).
            where_conditions: List[str] = ["o.tenant_id = $1"]
            if tips_only:
                where_conditions.append("o.tip_amount > 0")
            else:
                where_conditions.append(
                    "(o.pos_cart_id IS NOT NULL OR o.table_session_id IS NOT NULL "
                    "OR o.extra_attributes->>'source' = 'manual')"
                )
            params = [tenant_id]
            param_count = 1

            # Search filter
            if search and search_field:
                param_count += 1
                if search_field == "order_number":
                    where_conditions.append(f"CAST(o.order_number AS TEXT) ILIKE ${param_count}")
                    params.append(f"%{search}%")
                elif search_field == "customer_name":
                    where_conditions.append(f"p.name ILIKE ${param_count}")
                    params.append(f"%{search}%")
                elif search_field == "customer_phone":
                    where_conditions.append(f"p.phone_number ILIKE ${param_count}")
                    params.append(f"%{search}%")

            # Payment method filter
            if payment_method:
                param_count += 1
                where_conditions.append(f"o.payment_method = ${param_count}")
                params.append(payment_method)

            # Payment method ID filter (specific custom method)
            if payment_method_id:
                param_count += 1
                where_conditions.append(f"o.payment_method_id = ${param_count}::uuid")
                params.append(payment_method_id)

            # Status filter
            if status:
                param_count += 1
                where_conditions.append(f"o.status = ${param_count}")
                params.append(status)

            # Date range filter
            parsed_date_from = parse_date(date_from)
            parsed_date_to = parse_date(date_to)
            param_count = _append_local_date_bounds(
                where_conditions,
                params,
                param_count,
                "o.order_date",
                parsed_date_from,
                parsed_date_to,
                timezone_name,
            )

            # warocol.com#640 — tips-only filters (ignored when tips_only is False)
            if tips_only and member_id:
                param_count += 1
                where_conditions.append(f"o.served_by_member_id = ${param_count}::uuid")
                params.append(member_id)
            if tips_only and channel:
                if channel == 'online':
                    where_conditions.append("o.online_cart_id IS NOT NULL")
                elif channel == 'mesa':
                    where_conditions.append("o.table_session_id IS NOT NULL")
                elif channel == 'pos':
                    where_conditions.append("o.pos_cart_id IS NOT NULL AND o.table_session_id IS NULL")

            where_clause = " AND ".join(where_conditions)

            # Validate sort field (tips-only mode also accepts tip_amount)
            allowed_sort_fields = ["order_number", "order_date", "total_amount", "customer_name", "payment_method"]
            if tips_only:
                allowed_sort_fields.append("tip_amount")
            if sort_field not in allowed_sort_fields:
                sort_field = "order_date"

            sort_direction = "ASC" if sort_direction.lower() == "asc" else "DESC"

            # Map sort field to actual column
            sort_column_map = {
                "order_number": "o.order_number",
                "order_date": "o.order_date",
                "total_amount": "o.total_amount",
                "tip_amount": "o.tip_amount",
                "customer_name": "p.name",
                "payment_method": "o.payment_method"
            }
            sort_column = sort_column_map.get(sort_field, "o.order_date")

            # Get ALL rows without pagination. warocol.com#640 — tips-only mode
            # adds tip_amount/tip_source + mesero name + cart linkage so the CSV
            # can render channel and the tip-specific columns.
            if tips_only:
                orders_query = f"""
                    SELECT
                        o.id,
                        o.order_number,
                        o.order_date,
                        o.total_amount,
                        o.tip_amount,
                        o.tip_source,
                        o.status,
                        o.payment_method,
                        COALESCE(pmg.name, o.payment_method) AS payment_method_display,
                        p.name AS member_name,
                        o.online_cart_id,
                        o.table_session_id,
                        o.pos_cart_id
                    FROM orders o
                    LEFT JOIN tenant_members tm ON tm.id = o.served_by_member_id
                    LEFT JOIN profile p ON p.id = tm.user_id
                    LEFT JOIN payment_methods pm ON pm.id = o.payment_method_id AND pm.tenant_id = $1
                    LEFT JOIN payment_method_groups pmg ON pmg.id = pm.group_id AND pmg.tenant_id = $1
                    WHERE {where_clause}
                    ORDER BY {sort_column} {sort_direction}
                """
            else:
                orders_query = f"""
                    SELECT
                        o.id,
                        o.order_number,
                        o.order_date,
                        o.total_amount,
                        o.status,
                        o.payment_method,
                        COALESCE(pmg.name, o.payment_method) AS payment_method_display,
                        p.name as customer_name,
                        p.phone_number as customer_phone,
                        (
                            SELECT COUNT(*)
                            FROM order_items oi
                            WHERE oi.order_id = o.id
                        ) as items_count
                    FROM orders o
                    LEFT JOIN profile p ON o.customer_id = p.id
                    LEFT JOIN payment_methods pm ON pm.id = o.payment_method_id AND pm.tenant_id = $1
                    LEFT JOIN payment_method_groups pmg ON pmg.id = pm.group_id AND pmg.tenant_id = $1
                    WHERE {where_clause}
                    ORDER BY {sort_column} {sort_direction}
                """

            orders_rows = await conn.fetch(orders_query, *params)

            if not orders_rows:
                noun = _("tips") if tips_only else _("sales")
                raise APIError(_tr(_, "There are no {noun} to export with the selected filters", noun=noun), status_code=404)

            # Status labels
            status_labels = {
                'completed': 'Completada',
                'cancelled': 'Cancelada',
                'pending': 'Pendiente'
            }

            # Generate CSV with Excel-friendly formatting
            now = datetime.now(timezone.utc)
            local_now = now.astimezone(get_zoneinfo(timezone_name))
            output = io.StringIO()
            writer = csv.writer(output, delimiter=';')  # Use semicolon for better Excel compatibility

            total_sum = 0  # sum of total_amount (orders) or tip_amount (tips_only)
            completed_count = 0
            cancelled_count = 0

            if tips_only:
                # warocol.com#640 — tip-specific CSV shape
                writer.writerow([
                    'Numero Orden',
                    'Fecha',
                    'Hora',
                    'Canal',
                    'Mesero',
                    'Total Orden',
                    'Propina',
                    'Porcentaje',
                    'Metodo Pago',
                ])
                for row in orders_rows:
                    order_date_str = row['order_date'].strftime('%Y-%m-%d') if row['order_date'] else ''
                    order_time_str = row['order_date'].strftime('%H:%M:%S') if row['order_date'] else ''
                    total_amount = float(row['total_amount'])
                    tip_amount = float(row['tip_amount'])
                    pct = round(tip_amount / total_amount * 100, 2) if total_amount > 0 else 0
                    if row['online_cart_id']:
                        ch = 'online'
                    elif row['table_session_id']:
                        ch = 'mesa'
                    else:
                        ch = 'pos'
                    total_sum += tip_amount
                    completed_count += 1
                    writer.writerow([
                        row['order_number'],
                        order_date_str,
                        order_time_str,
                        ch,
                        row['member_name'] or 'Sin asignar',
                        total_amount,
                        tip_amount,
                        f"{pct}%",
                        row['payment_method_display'] or row['payment_method'] or '',
                    ])
            else:
                # Standard orders export (unchanged shape)
                writer.writerow([
                    'Numero Orden',
                    'Fecha',
                    'Hora',
                    'Cliente',
                    'Telefono',
                    'Items',
                    'Metodo Pago',
                    'Total',
                    'Estado'
                ])
                for row in orders_rows:
                    order_date_str = row['order_date'].strftime('%Y-%m-%d') if row['order_date'] else ''
                    order_time_str = row['order_date'].strftime('%H:%M:%S') if row['order_date'] else ''
                    total_amount = float(row['total_amount'])

                    if row['status'] == 'completed':
                        total_sum += total_amount
                        completed_count += 1
                    elif row['status'] == 'cancelled':
                        cancelled_count += 1

                    writer.writerow([
                        row['order_number'],
                        order_date_str,
                        order_time_str,
                        row['customer_name'] or 'Sin nombre',
                        row['customer_phone'] or '',
                        row['items_count'],
                        row['payment_method_display'] or row['payment_method'] or '',
                        total_amount,
                        status_labels.get(row['status'], row['status'])
                    ])

            csv_content = output.getvalue()
            output.close()

            # Build filter description for email
            filter_desc = []
            if date_from and date_to:
                filter_desc.append(_tr(_, "Period: {date_from} to {date_to}", date_from=date_from, date_to=date_to))
            elif date_from:
                filter_desc.append(_tr(_, "From: {date_from}", date_from=date_from))
            elif date_to:
                filter_desc.append(_tr(_, "To: {date_to}", date_to=date_to))
            if status:
                filter_desc.append(_tr(_, "Status: {status}", status=_(status_labels.get(status, status))))
            if payment_method:
                filter_desc.append(_tr(_, "Payment method: {payment_method}", payment_method=payment_method))
            if search:
                filter_desc.append(_tr(_, "Search: {search}", search=search))

            filter_text = "\n".join(filter_desc) if filter_desc else _("No filters applied")

            date_str = local_now.strftime('%Y-%m-%d_%H%M')

            if tips_only:
                # warocol.com#640 — tip-specific email copy + filename + subject
                email_body = _tr(_, """Hello {user_name}!

Here is the tips report you requested.

SUMMARY
-------
Total tips exported: {count}
Total tips amount: {total}
Generated on: {generated_at}

APPLIED FILTERS
-----------------
{filter_text}

The CSV file with the tips detail is attached.
You can open it with Excel or Google Sheets.

---
{user_name} from {tenant_name}
Colombian technology for the world.
""",
                    user_name=user_name,
                    count=len(orders_rows),
                    total=format_money(total_sum, locale, currency_code),
                    generated_at=format_localized_datetime(now, locale, timezone_name),
                    filter_text=filter_text,
                    tenant_name=tenant_name,
                )
                filename = f"propinas_{date_str}.csv"
                subject = _tr(_, "Tips report - {date}", date=date_str)
            else:
                email_body = _tr(_, """Hello {user_name}!

Here is the sales report you requested.

SUMMARY
-------
Total sales exported: {count}
Completed sales: {completed_count}
Cancelled sales: {cancelled_count}
Total amount (completed): {total}
Generated on: {generated_at}

APPLIED FILTERS
-----------------
{filter_text}

The CSV file with the sales detail is attached.
You can open it with Excel or Google Sheets.

---
{user_name} from {tenant_name}
Colombian technology for the world.
""",
                    user_name=user_name,
                    count=len(orders_rows),
                    completed_count=completed_count,
                    cancelled_count=cancelled_count,
                    total=format_money(total_sum, locale, currency_code),
                    generated_at=format_localized_datetime(now, locale, timezone_name),
                    filter_text=filter_text,
                    tenant_name=tenant_name,
                )
                filename = f"ventas_{date_str}.csv"
                subject = _tr(_, "Sales report - {date}", date=date_str)

            # Send email with CSV attachment
            success = await ses_service.send_email_with_attachment(
                from_email=await resolve_sender_email_for_tenant(tenant_id),
                from_name=_("Waro Colombia - Reports"),
                to_emails=[user_email],
                subject=subject,
                text_body=email_body,
                attachment_data=csv_content.encode('utf-8'),
                attachment_filename=filename,
                attachment_type="text/csv"
            )

            if not success:
                raise APIError(_("Could not send the email. Please try again."), status_code=500)

            return {
                "success": True,
                "message": _tr(_, "Report sent to {email}", email=user_email),
                "data": {
                    "email": user_email,
                    "orders_count": len(orders_rows),
                    "total_sales": total_sum
                }
            }

    except (AuthenticationError, APIError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error exporting orders: {str(e)}")
        raise APIError(f"Error al exportar ventas: {str(e)}", status_code=500)


async def delete_order_item(
    request: Request,
    order_id: UUID,
    item_id: UUID
) -> dict:
    """
    Delete an order item and its associated modifiers.
    Returns ingredients to inventory (reverse of POS consumption).
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                # Verify order exists and get order number
                order_query = """
                    SELECT id, order_number, order_date FROM orders
                    WHERE id = $1 AND tenant_id = $2 AND pos_cart_id IS NOT NULL
                """
                order_row = await conn.fetchrow(order_query, order_id, tenant_id)

                if not order_row:
                    raise APIError("Order not found", status_code=404)

                # Guard: block mutation if order falls in a closed monthly accounting period (#362)
                await assert_order_not_in_closed_monthly_period(conn, tenant_id, order_row['order_date'])

                order_number = order_row['order_number']

                # Get item details before deletion
                item_query = """
                    SELECT oi.id, oi.product_id, oi.quantity, p.name as product_name
                    FROM order_items oi
                    JOIN product p ON oi.product_id = p.id
                    WHERE oi.id = $1 AND oi.order_id = $2
                """
                item_row = await conn.fetchrow(item_query, item_id, order_id)

                if not item_row:
                    raise APIError("Order item not found", status_code=404)

                # Check if this is the last item - don't allow deletion
                items_count_query = """
                    SELECT COUNT(*) as count FROM order_items WHERE order_id = $1
                """
                items_count_row = await conn.fetchrow(items_count_query, order_id)
                if items_count_row['count'] <= 1:
                    raise APIError("No se puede eliminar el único producto de la venta", status_code=400)

                product_id = item_row['product_id']
                item_quantity = float(item_row['quantity'])
                product_name = item_row['product_name']

                _, return_modifier_inventory_for_order_item, return_order_item_inventory_from_snapshots = _pos_modifier_inventory_helpers()
                returned_from_snapshots = await return_order_item_inventory_from_snapshots(
                    conn,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    order_id=order_id,
                    order_number=order_number,
                    order_item_id=item_id,
                    reason_detail=f"Devolución por eliminación de {int(item_quantity)}x {product_name}",
                )

                if not returned_from_snapshots:
                    ingredients = await conn.fetch(_INGREDIENTS_QUERY, product_id)
                    for ingredient in ingredients:
                        quantity_to_return = item_quantity * float(ingredient['quantity'])
                        await _return_ingredient_to_stock(
                            conn, tenant_id, user_id, order_id, order_number,
                            ingredient['ingredient_id'],
                            quantity_to_return,
                            ingredient['unit'],
                            ingredient['ingredient_name'],
                            f"Devolución por eliminación de {int(item_quantity)}x {product_name}",
                        )

                    modifiers = await conn.fetch(
                        """
                        SELECT
                            oim.modifier_id,
                            oim.modifier_name,
                            oim.quantity AS modifier_qty
                        FROM order_item_modifiers oim
                        WHERE oim.order_item_id = $1
                        """,
                        item_id,
                    )
                    for modifier in modifiers:
                        if not modifier['modifier_id']:
                            continue
                        modifier_qty = float(modifier['modifier_qty']) if modifier['modifier_qty'] else 1.0
                        await return_modifier_inventory_for_order_item(
                            conn,
                            tenant_id=tenant_id,
                            user_id=user_id,
                            order_id=order_id,
                            order_number=order_number,
                            order_item_id=item_id,
                            item_quantity=item_quantity,
                            modifier_id=modifier['modifier_id'],
                            modifier_qty=modifier_qty,
                            modifier_name=modifier['modifier_name'],
                            product_name=product_name,
                        )

                # Delete associated modifiers (foreign key constraint)
                await conn.execute(
                    "DELETE FROM order_item_modifiers WHERE order_item_id = $1",
                    item_id
                )

                # 4. Delete the order item
                await conn.execute(
                    "DELETE FROM order_items WHERE id = $1",
                    item_id
                )

                # 5. Update order total
                new_total_query = """
                    SELECT COALESCE(SUM(subtotal), 0) as new_total
                    FROM order_items
                    WHERE order_id = $1
                """
                new_total_row = await conn.fetchrow(new_total_query, order_id)
                new_total = float(new_total_row['new_total'])

                await conn.execute(
                    "UPDATE orders SET total_amount = $1 WHERE id = $2",
                    new_total, order_id
                )

                logger.info(f"Order item deleted and inventory restored for Order #{order_number}")

                return {
                    "success": True,
                    "message": "Item eliminado y stock actualizado",
                    "data": {
                        "new_total": new_total
                    }
                }

    except (AuthenticationError, APIError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error deleting order item: {str(e)}")
        raise APIError(f"Error deleting order item: {str(e)}", status_code=500)


_INGREDIENTS_QUERY = """
    SELECT pr.ingredient_id, pr.quantity, pr.unit, i.name AS ingredient_name
    FROM product_recipes pr
    JOIN ingredients i ON pr.ingredient_id = i.id
    WHERE pr.product_id = $1
    UNION ALL
    -- Issue #517: multiply by pbr.quantity
    SELECT brt.ingredient_id, brt.base_quantity * pbr.quantity AS quantity, brt.unit, i.name AS ingredient_name
    FROM product_base_recipes pbr
    JOIN base_recipe_templates brt ON pbr.product_base_type_id = brt.product_base_type_id
    JOIN ingredients i ON brt.ingredient_id = i.id
    WHERE pbr.product_id = $1
"""


async def _deduct_stock_for_status_update(conn, order_id, tenant_id, user_id, order_number: int) -> None:
    """Deduct ingredient stock when an order transitions to completed via status update."""
    items = await conn.fetch(
        """SELECT oi.product_id, oi.quantity, p.name AS product_name
           FROM order_items oi
           JOIN product p ON p.id = oi.product_id
           WHERE oi.order_id = $1""",
        order_id,
    )
    for item in items:
        ingredients = await conn.fetch(_INGREDIENTS_QUERY, item["product_id"])
        for ing in ingredients:
            qty = _inventory_quantity(Decimal(str(item["quantity"])) * Decimal(str(ing["quantity"])))
            stock_row = await conn.fetchrow(
                "SELECT current_stock FROM tenant_inventory WHERE ingredient_id = $1 AND tenant_id = $2 FOR UPDATE",
                ing["ingredient_id"], tenant_id,
            )
            prev = float(stock_row["current_stock"]) if stock_row else 0.0
            new = _inventory_quantity(Decimal(str(prev)) - Decimal(str(qty)))
            if stock_row:
                await conn.execute(
                    "UPDATE tenant_inventory SET current_stock = $1, last_updated = NOW() WHERE ingredient_id = $2 AND tenant_id = $3",
                    new, ing["ingredient_id"], tenant_id,
                )
            else:
                await conn.execute(
                    "INSERT INTO tenant_inventory (tenant_id, ingredient_id, current_stock, minimum_stock, last_updated) VALUES ($1, $2, $3, 0, NOW())",
                    tenant_id, ing["ingredient_id"], -qty,
                )
            await conn.execute(
                """INSERT INTO tenant_ingredient_movements (
                    tenant_id, ingredient_id, movement_type, quantity_change, unit,
                    previous_stock, new_stock, reference_table, reference_id, reason, created_by, created_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,NOW())""",
                tenant_id, ing["ingredient_id"], "consumption", -qty,
                ing["unit"], prev, new, "orders", order_id,
                f"Venta #{order_number} completada: {item['quantity']}x {item['product_name']}",
                user_id,
            )
            logger.info(f"Stock deducted (status update): {ing['ingredient_name']} -{qty}{ing['unit']} (Orden #{order_number})")


async def _return_stock_for_order_cancellation(conn, order_id, tenant_id, user_id, order_number: int) -> None:
    """Return ingredient stock when a completed order is cancelled or rolled back to pending."""
    items = await conn.fetch(
        """SELECT oi.product_id, oi.quantity, p.name AS product_name
           FROM order_items oi
           JOIN product p ON p.id = oi.product_id
           WHERE oi.order_id = $1""",
        order_id,
    )
    for item in items:
        ingredients = await conn.fetch(_INGREDIENTS_QUERY, item["product_id"])
        for ing in ingredients:
            qty = _inventory_quantity(Decimal(str(item["quantity"])) * Decimal(str(ing["quantity"])))
            await _return_ingredient_to_stock(
                conn, tenant_id, user_id, order_id, order_number,
                ing["ingredient_id"], qty, ing["unit"], ing["ingredient_name"],
                f"Cancelación: {item['quantity']}x {item['product_name']}"
            )
            logger.info(f"Stock returned (cancellation): {ing['ingredient_name']} +{qty}{ing['unit']} (Orden #{order_number})")


async def _return_ingredient_to_stock(
    conn, tenant_id, user_id, order_id, order_number,
    ingredient_id, quantity, unit, ingredient_name, reason_detail
):
    """
    Helper function to return an ingredient to stock and create movement record.
    """
    # Get current stock
    stock_row = await conn.fetchrow(
        """
        SELECT current_stock FROM tenant_inventory
        WHERE ingredient_id = $1 AND tenant_id = $2
        FOR UPDATE
        """,
        ingredient_id, tenant_id
    )

    previous_stock = float(stock_row['current_stock']) if stock_row else 0.0
    new_stock = _inventory_quantity(Decimal(str(previous_stock)) + Decimal(str(quantity)))

    # Update or create inventory record
    if stock_row:
        await conn.execute(
            """
            UPDATE tenant_inventory
            SET current_stock = $1, last_updated = NOW()
            WHERE ingredient_id = $2 AND tenant_id = $3
            """,
            new_stock, ingredient_id, tenant_id
        )
    else:
        await conn.execute(
            """
            INSERT INTO tenant_inventory (
                tenant_id, ingredient_id, current_stock, minimum_stock, last_updated
            ) VALUES ($1, $2, $3, 0, NOW())
            """,
            tenant_id, ingredient_id, quantity
        )

    # Create movement record
    await conn.execute(
        """
        INSERT INTO tenant_ingredient_movements (
            tenant_id, ingredient_id, movement_type,
            quantity_change, unit, previous_stock, new_stock,
            reference_table, reference_id, reason, created_by, created_at
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW())
        """,
        tenant_id,
        ingredient_id,
        'return',  # New movement type for returns
        quantity,  # Positive = adding back to stock
        unit,
        previous_stock,
        new_stock,
        'orders',
        order_id,
        f"Ajuste Venta #{order_number}: {reason_detail} ({ingredient_name})",
        user_id
    )

    logger.info(f"Inventory restored: {ingredient_name} +{quantity}{unit} (Order #{order_number})")


async def delete_order_item_modifier(
    request: Request,
    order_id: UUID,
    item_id: UUID,
    modifier_id: UUID
) -> dict:
    """
    Delete a modifier from an order item.
    Returns ingredient to inventory if modifier has linked ingredient.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                # Verify order exists and get order number
                order_query = """
                    SELECT id, order_number, order_date FROM orders
                    WHERE id = $1 AND tenant_id = $2 AND pos_cart_id IS NOT NULL
                """
                order_row = await conn.fetchrow(order_query, order_id, tenant_id)

                if not order_row:
                    raise APIError("Order not found", status_code=404)

                # Guard: block mutation if order falls in a closed monthly accounting period (#362)
                await assert_order_not_in_closed_monthly_period(conn, tenant_id, order_row['order_date'])

                order_number = order_row['order_number']

                # Get item details
                item_query = """
                    SELECT oi.id, oi.quantity, p.name as product_name
                    FROM order_items oi
                    JOIN product p ON oi.product_id = p.id
                    WHERE oi.id = $1 AND oi.order_id = $2
                """
                item_row = await conn.fetchrow(item_query, item_id, order_id)

                if not item_row:
                    raise APIError("Order item not found", status_code=404)

                item_quantity = float(item_row['quantity'])
                product_name = item_row['product_name']

                # Get modifier details with ingredient info
                modifier_query = """
                    SELECT
                        oim.id,
                        oim.price_at_purchase,
                        oim.modifier_name,
                        oim.quantity as modifier_qty,
                        oim.included_quantity_at_purchase,
                        oim.modifier_id as original_modifier_id,
                        m.ingredient_id,
                        m.ingredient_quantity,
                        m.ingredient_unit,
                        i.name as ingredient_name
                    FROM order_item_modifiers oim
                    LEFT JOIN modifiers m ON oim.modifier_id = m.id
                    LEFT JOIN ingredients i ON m.ingredient_id = i.id
                    WHERE oim.id = $1 AND oim.order_item_id = $2
                """
                modifier_row = await conn.fetchrow(modifier_query, modifier_id, item_id)

                if not modifier_row:
                    raise APIError("Modifier not found", status_code=404)

                modifier_name = modifier_row['modifier_name']
                modifier_qty = float(modifier_row['modifier_qty']) if modifier_row['modifier_qty'] else 1.0

                _, return_modifier_inventory_for_order_item, _ = _pos_modifier_inventory_helpers()
                if modifier_row['original_modifier_id']:
                    await return_modifier_inventory_for_order_item(
                        conn,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        order_id=order_id,
                        order_number=order_number,
                        order_item_id=item_id,
                        item_quantity=item_quantity,
                        modifier_id=modifier_row['original_modifier_id'],
                        modifier_qty=modifier_qty,
                        modifier_name=modifier_name,
                        product_name=product_name,
                    )

                # Delete the modifier
                await conn.execute(
                    "DELETE FROM order_item_modifiers WHERE id = $1",
                    modifier_id
                )

                # Update item subtotal
                new_item_subtotal_query = """
                    SELECT
                        oi.price_at_purchase * oi.quantity +
                        COALESCE(SUM(
                            oim.price_at_purchase
                            * GREATEST(
                                COALESCE(oim.quantity, 1)
                                - oim.included_quantity_at_purchase,
                                0
                              )
                            * oi.quantity
                        ), 0) as new_subtotal
                    FROM order_items oi
                    LEFT JOIN order_item_modifiers oim ON oim.order_item_id = oi.id
                    WHERE oi.id = $1
                    GROUP BY oi.id, oi.price_at_purchase, oi.quantity
                """
                new_subtotal_row = await conn.fetchrow(new_item_subtotal_query, item_id)
                new_item_subtotal = float(new_subtotal_row['new_subtotal']) if new_subtotal_row else 0

                await conn.execute(
                    "UPDATE order_items SET subtotal = $1 WHERE id = $2",
                    new_item_subtotal, item_id
                )

                # Update order total
                new_total_query = """
                    SELECT COALESCE(SUM(subtotal), 0) as new_total
                    FROM order_items
                    WHERE order_id = $1
                """
                new_total_row = await conn.fetchrow(new_total_query, order_id)
                new_total = float(new_total_row['new_total'])

                await conn.execute(
                    "UPDATE orders SET total_amount = $1 WHERE id = $2",
                    new_total, order_id
                )

                logger.info(f"Modifier {modifier_name} deleted from Order #{order_number}")

                return {
                    "success": True,
                    "message": "Modificador eliminado y stock actualizado",
                    "data": {
                        "new_item_subtotal": new_item_subtotal,
                        "new_total": new_total
                    }
                }

    except (AuthenticationError, APIError) as e:
        raise e
    except Exception as e:
        logger.error(f"Error deleting order item modifier: {str(e)}")
        raise APIError(f"Error deleting order item modifier: {str(e)}", status_code=500)


async def get_sales_flow(
    request: Request,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    payment_method: Optional[str] = None,
    status: Optional[str] = None
) -> dict:
    """
    Get sales flow data with intelligent comparison and grouping
    - Ranges ≤30 days: Compare with previous period
    - Ranges >30 days: Compare with same period last year
    - Auto-grouping: hourly (≤3 days), daily (4-90 days)
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        # Parse dates and calculate periods
        from datetime import datetime, timedelta

        async with get_db_connection() as conn:
            timezone_name = await resolve_tenant_timezone(conn, tenant_id)
            parsed_date_from = parse_date(date_from)
            parsed_date_to = parse_date(date_to)

            # Default to year-to-date if no dates provided (same as metrics cards)
            if not parsed_date_from or not parsed_date_to:
                today = tenant_today(timezone_name)
                parsed_date_to = today
                # Start from January 1st of current year
                parsed_date_from = today.replace(month=1, day=1)

            # Calculate range duration
            days_diff = (parsed_date_to - parsed_date_from).days + 1

            # Determine comparison period and grouping
            if days_diff <= 30:
                # Compare with previous period
                comparison_date_to = parsed_date_from - timedelta(days=1)
                comparison_date_from = comparison_date_to - timedelta(days=days_diff - 1)
                comparison_label = "Período Anterior"
            else:
                # Compare with same period last year
                comparison_date_from = parsed_date_from.replace(year=parsed_date_from.year - 1)
                comparison_date_to = parsed_date_to.replace(year=parsed_date_to.year - 1)
                comparison_label = "Año Anterior"

            # Determine grouping: hourly (≤3 days), daily (>3 days)
            group_by = 'hour' if days_diff <= 3 else 'day'

            # Build WHERE conditions
            where_conditions = ["tenant_id = $1", ANALYTICS_SALES_FILTER]
            params = [tenant_id]
            param_count = 1

            # Add filters
            if payment_method:
                param_count += 1
                where_conditions.append(f"payment_method = ${param_count}")
                params.append(payment_method)

            if status:
                param_count += 1
                where_conditions.append(f"status = ${param_count}")
                params.append(status)
            else:
                # Default to completed if no status filter
                where_conditions.append("status = 'completed'")

            where_clause = " AND ".join(where_conditions)

            # Build query based on grouping
            if group_by == 'hour':
                # Hourly grouping
                param_count += 1
                date_from_param_idx = param_count
                param_count += 1
                date_to_param_idx = param_count
                param_count += 1
                comp_from_param_idx = param_count
                param_count += 1
                comp_to_param_idx = param_count
                param_count += 1
                timezone_param_idx = param_count

                params.extend([parsed_date_from, parsed_date_to, comparison_date_from, comparison_date_to, timezone_name])

                query = f"""
                    WITH hours AS (
                        SELECT generate_series(0, 23) AS hour
                    ),
                    current_period AS (
                        SELECT
                            EXTRACT(HOUR FROM order_date AT TIME ZONE ${timezone_param_idx}) AS hour,
                            SUM(total_amount) AS sales
                        FROM orders
                        WHERE {where_clause}
                          AND DATE(order_date AT TIME ZONE ${timezone_param_idx}) >= ${date_from_param_idx}
                          AND DATE(order_date AT TIME ZONE ${timezone_param_idx}) <= ${date_to_param_idx}
                        GROUP BY EXTRACT(HOUR FROM order_date AT TIME ZONE ${timezone_param_idx})
                    ),
                    comparison_period AS (
                        SELECT
                            EXTRACT(HOUR FROM order_date AT TIME ZONE ${timezone_param_idx}) AS hour,
                            SUM(total_amount) AS sales
                        FROM orders
                        WHERE {where_clause}
                          AND DATE(order_date AT TIME ZONE ${timezone_param_idx}) >= ${comp_from_param_idx}
                          AND DATE(order_date AT TIME ZONE ${timezone_param_idx}) <= ${comp_to_param_idx}
                        GROUP BY EXTRACT(HOUR FROM order_date AT TIME ZONE ${timezone_param_idx})
                    )
                    SELECT
                        h.hour,
                        COALESCE(cp.sales, 0) AS current_sales,
                        COALESCE(cmp.sales, 0) AS comparison_sales
                    FROM hours h
                    LEFT JOIN current_period cp ON h.hour = cp.hour
                    LEFT JOIN comparison_period cmp ON h.hour = cmp.hour
                    WHERE COALESCE(cp.sales, 0) > 0 OR COALESCE(cmp.sales, 0) > 0
                    ORDER BY h.hour
                """

                rows = await conn.fetch(query, *params)

                # Format data for hourly
                data = []
                for row in rows:
                    hour = int(row['hour'])
                    if hour == 0:
                        label = '12am'
                    elif hour < 12:
                        label = f'{hour}am'
                    elif hour == 12:
                        label = '12pm'
                    else:
                        label = f'{hour-12}pm'

                    data.append({
                        'name': label,
                        'sales': round(float(row['current_sales'])),
                        'salesYesterday': round(float(row['comparison_sales']))
                    })

            else:  # day grouping
                # Daily grouping - optimized without recursive CTEs
                param_count += 1
                date_from_param_idx = param_count
                param_count += 1
                date_to_param_idx = param_count
                param_count += 1
                comp_from_param_idx = param_count
                param_count += 1
                comp_to_param_idx = param_count
                param_count += 1
                timezone_param_idx = param_count

                params.extend([parsed_date_from, parsed_date_to, comparison_date_from, comparison_date_to, timezone_name])

                # Use generate_series directly in the query (more efficient than recursive CTE)
                query = f"""
                    WITH date_series AS (
                        SELECT generate_series(
                            ${date_from_param_idx}::date,
                            ${date_to_param_idx}::date,
                            '1 day'::interval
                        )::date AS day
                    ),
                    current_period AS (
                        SELECT
                            DATE(order_date AT TIME ZONE ${timezone_param_idx}) AS day,
                            SUM(total_amount) AS sales
                        FROM orders
                        WHERE {where_clause}
                          AND DATE(order_date AT TIME ZONE ${timezone_param_idx}) >= ${date_from_param_idx}
                          AND DATE(order_date AT TIME ZONE ${timezone_param_idx}) <= ${date_to_param_idx}
                        GROUP BY DATE(order_date AT TIME ZONE ${timezone_param_idx})
                    ),
                    comparison_period AS (
                        SELECT
                            DATE(order_date AT TIME ZONE ${timezone_param_idx}) AS day,
                            SUM(total_amount) AS sales
                        FROM orders
                        WHERE {where_clause}
                          AND DATE(order_date AT TIME ZONE ${timezone_param_idx}) >= ${comp_from_param_idx}
                          AND DATE(order_date AT TIME ZONE ${timezone_param_idx}) <= ${comp_to_param_idx}
                        GROUP BY DATE(order_date AT TIME ZONE ${timezone_param_idx})
                    )
                    SELECT
                        ds.day,
                        COALESCE(cp.sales, 0) AS current_sales,
                        COALESCE(cmp.sales, 0) AS comparison_sales
                    FROM date_series ds
                    LEFT JOIN current_period cp ON ds.day = cp.day
                    LEFT JOIN comparison_period cmp ON
                        cmp.day = (ds.day - (${date_from_param_idx}::date - ${comp_from_param_idx}::date))
                    ORDER BY ds.day
                """

                rows = await conn.fetch(query, *params)

                # Format data for daily
                data = []
                for row in rows:
                    day = row['day']
                    label = day.strftime('%d/%m')

                    data.append({
                        'name': label,
                        'sales': round(float(row['current_sales'])),
                        'salesYesterday': round(float(row['comparison_sales']))
                    })

            # If no data, return placeholder
            if not data:
                data = [
                    { 'name': '12pm', 'sales': 0, 'salesYesterday': 0 },
                    { 'name': '2pm', 'sales': 0, 'salesYesterday': 0 },
                    { 'name': '4pm', 'sales': 0, 'salesYesterday': 0 },
                    { 'name': '6pm', 'sales': 0, 'salesYesterday': 0 },
                    { 'name': '8pm', 'sales': 0, 'salesYesterday': 0 },
                    { 'name': '10pm', 'sales': 0, 'salesYesterday': 0 },
                ]

            return {
                "success": True,
                "data": data,
                "metadata": {
                    "grouping": group_by,
                    "comparison_type": "previous_period" if days_diff <= 30 else "year_over_year",
                    "comparison_label": comparison_label,
                    "days_in_range": days_diff
                }
            }

    except AuthenticationError as e:
        raise e
    except Exception as e:
        logger.error(f"Error getting sales flow: {str(e)}")
        raise APIError(f"Error getting sales flow: {str(e)}", status_code=500)


async def create_manual_order(
    request: Request,
    order_date: str,
    payment_method: str,
    items: List[dict],
    customer_id: Optional[str] = None,
    payment_method_id: Optional[str] = None,
    discount_type: Optional[str] = None,
    discount_value: Optional[float] = None,
    payments: Optional[List[dict]] = None,
) -> dict:
    """
    Create an order manually with a custom date, bypassing the POS cart.
    Stores extra_attributes = {"source": "manual"} to identify it.
    """
    import json
    from datetime import datetime

    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        if not items:
            raise APIError("At least one item is required", status_code=400)

        try:
            order_datetime = datetime.fromisoformat(order_date)
        except ValueError:
            raise APIError("Invalid order_date format. Expected YYYY-MM-DDTHH:MM", status_code=400)

        customer_uuid = UUID(customer_id) if customer_id else None
        payment_method_uuid = UUID(payment_method_id) if payment_method_id else None
        split_payments = payments or []
        uses_credit = payment_method == "credit" or any(
            payment.get("payment_method") == "credit"
            for payment in split_payments
        )

        if uses_credit and not customer_uuid:
            raise APIError(
                "El pago a crédito requiere un cliente identificado",
                status_code=400,
            )

        if payment_method == "customer_wallet":
            if not customer_uuid:
                raise APIError("La billetera requiere un cliente identificado", status_code=400)
            if payment_method_uuid:
                raise APIError("payment_method_id no aplica al método billetera del cliente", status_code=400)

        for payment in split_payments:
            if payment.get("payment_method") == "customer_wallet" and not customer_uuid:
                raise APIError("La billetera requiere un cliente identificado", status_code=400)
            if payment.get("payment_method") == "customer_wallet" and payment.get("payment_method_id"):
                raise APIError("payment_method_id no aplica al método billetera del cliente", status_code=400)
            if payment.get("cash_received") is not None:
                if payment.get("payment_method") != "cash":
                    raise APIError("cash_received solo aplica a pagos en efectivo", status_code=400)
                if float(payment["cash_received"]) < float(payment["amount"]):
                    raise APIError("Efectivo recibido debe ser mayor o igual al monto", status_code=400)
            if payment.get("payment_method_id"):
                try:
                    UUID(payment["payment_method_id"])
                except ValueError:
                    raise APIError("payment_method_id no es un UUID válido", status_code=422)

        async with get_db_connection() as conn:
            timezone_name = await resolve_tenant_timezone(conn, tenant_id)
            async with conn.transaction():
                # Guard: block creation if order_date falls in a closed monthly accounting period (#362)
                await assert_order_not_in_closed_monthly_period(conn, tenant_id, order_datetime)

                if uses_credit and customer_uuid:
                    customer_check = await conn.fetchrow(
                        "SELECT phone_number FROM profile WHERE id = $1",
                        customer_uuid,
                    )
                    if (
                        not customer_check
                        or customer_check["phone_number"] == "0000000000"
                    ):
                        raise APIError(
                            "El pago a crédito requiere un cliente identificado (no anónimo)",
                            status_code=400,
                        )

                if payment_method == "customer_wallet" and customer_uuid:
                    from app.services.customer_wallet_service import assert_wallet_customer_identified

                    await assert_wallet_customer_identified(conn, customer_uuid)

                def modifier_quantity(modifier: dict) -> float:
                    return float(modifier.get("quantity") or 1)

                def modifier_unit_total(modifier: dict) -> float:
                    return float(
                        modifier_line_subtotal(
                            modifier.get("price", 0),
                            modifier_quantity(modifier),
                            modifier.get("included_quantity", 0),
                        )
                    )

                for item in items:
                    item["modifiers"] = await resolve_modifier_selections(
                        conn,
                        UUID(str(item["product_id"])),
                        item.get("modifiers", []),
                    )

                # Compute total server-side — never trust client total.
                gross_total = sum(
                    float(item["quantity"]) * (
                        float(item["unit_price"])
                        + sum(modifier_unit_total(m) for m in item.get("modifiers", []))
                    )
                    for item in items
                )
                if gross_total < 0:
                    raise APIError("Total de venta inválido", status_code=400)

                normalized_discount_type = discount_type if discount_value is not None else None
                normalized_discount_value = float(discount_value) if discount_value is not None else None
                discount_amount = 0.0
                if normalized_discount_type:
                    if normalized_discount_type not in ("percent", "fixed"):
                        raise APIError("discount_type debe ser 'percent' o 'fixed'", status_code=400)
                    if normalized_discount_value is None or normalized_discount_value <= 0:
                        raise APIError("discount_value debe ser mayor a 0", status_code=400)
                    if normalized_discount_type == "percent":
                        if normalized_discount_value > 100:
                            raise APIError("El descuento porcentual no puede superar el 100%", status_code=400)
                        discount_amount = gross_total * normalized_discount_value / 100
                    else:
                        discount_amount = normalized_discount_value
                    if discount_amount - gross_total > 0.01:
                        raise APIError("El descuento no puede superar el subtotal", status_code=400)
                    discount_amount = round(discount_amount, 2)

                total_amount = round(max(0.0, gross_total - discount_amount), 2)

                if split_payments:
                    paid_total = round(sum(float(p["amount"]) for p in split_payments), 2)
                    if abs(paid_total - total_amount) > 0.01:
                        raise APIError(
                            f"Los pagos divididos ({paid_total}) deben sumar el total ({total_amount})",
                            status_code=400,
                        )

                payment_status = (
                    "paid"
                    if split_payments
                    else ("credit" if payment_method == "credit" else "paid")
                )

                order_row = await conn.fetchrow(
                    """
                    INSERT INTO orders (
                        tenant_id, customer_id, payment_method, payment_method_id,
                        order_date, total_amount, status, payment_status,
                        discount_type, discount_value, discount_amount, extra_attributes
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, 'completed', $7, $8, $9, $10, $11)
                    RETURNING id, order_number, order_date, created_at
                    """,
                    tenant_id,
                    customer_uuid,
                    payment_method,
                    payment_method_uuid,
                    order_datetime,
                    total_amount,
                    payment_status,
                    normalized_discount_type,
                    normalized_discount_value,
                    discount_amount or None,
                    json.dumps({"source": "manual"})
                )

                order_id = order_row["id"]

                deduct_modifier_inventory, _, _ = _pos_modifier_inventory_helpers()
                capture_order_item_ingredients = _pos_order_item_ingredient_snapshot_helper()

                for item in items:
                    modifiers = item.get("modifiers", [])
                    modifiers_total = sum(modifier_unit_total(m) for m in modifiers)
                    subtotal = float(item["quantity"]) * (
                        float(item["unit_price"]) + modifiers_total
                    )
                    product_id = UUID(item["product_id"])
                    order_item_row = await conn.fetchrow(
                        """
                        INSERT INTO order_items (
                            order_id, product_id, quantity, price_at_purchase, subtotal
                        )
                        VALUES ($1, $2, $3, $4, $5)
                        RETURNING id
                        """,
                        order_id,
                        product_id,
                        float(item["quantity"]),
                        float(item["unit_price"]),
                        subtotal
                    )
                    order_item_id = order_item_row["id"]

                    for modifier in modifiers:
                        modifier_qty = modifier_quantity(modifier)
                        await conn.execute(
                            """
                            INSERT INTO order_item_modifiers (
                                order_item_id, modifier_id, modifier_name,
                                price_at_purchase, quantity,
                                included_quantity_at_purchase
                            )
                            VALUES ($1, $2, $3, $4, $5, $6)
                            """,
                            order_item_id,
                            UUID(str(modifier["id"])),
                            modifier["name"],
                            float(modifier.get("price", 0)),
                            modifier_qty,
                            modifier.get("included_quantity", 0),
                        )

                        await deduct_modifier_inventory(
                            conn,
                            tenant_id=tenant_id,
                            user_id=user_id,
                            order_id=order_id,
                            order_item_id=order_item_id,
                            order_number=order_row["order_number"],
                            item_quantity=float(item["quantity"]),
                            modifier=modifier,
                            modifier_qty=modifier_qty,
                        )

                    # Deduct inventory for product ingredients (direct + base recipes)
                    ingredients = await conn.fetch(
                        """
                        SELECT pr.ingredient_id, pr.quantity, pr.unit, i.name AS ingredient_name
                        FROM product_recipes pr
                        JOIN ingredients i ON pr.ingredient_id = i.id
                        WHERE pr.product_id = $1

                        UNION ALL

                        -- Issue #517: multiply by pbr.quantity
                        SELECT brt.ingredient_id, brt.base_quantity * pbr.quantity AS quantity, brt.unit, i.name AS ingredient_name
                        FROM product_base_recipes pbr
                        JOIN base_recipe_templates brt ON pbr.product_base_type_id = brt.product_base_type_id
                        JOIN ingredients i ON brt.ingredient_id = i.id
                        WHERE pbr.product_id = $1
                        """,
                        product_id
                    )

                    for ingredient in ingredients:
                        quantity_to_deduct = _inventory_quantity(
                            Decimal(str(item["quantity"])) * Decimal(str(ingredient["quantity"]))
                        )
                        stock_row = await conn.fetchrow(
                            "SELECT current_stock FROM tenant_inventory WHERE ingredient_id = $1 AND tenant_id = $2",
                            ingredient["ingredient_id"],
                            tenant_id
                        )
                        previous_stock = float(stock_row["current_stock"]) if stock_row else 0.0
                        new_stock = _inventory_quantity(
                            Decimal(str(previous_stock)) - Decimal(str(quantity_to_deduct))
                        )

                        if stock_row:
                            await conn.execute(
                                "UPDATE tenant_inventory SET current_stock = $1, last_updated = NOW() WHERE ingredient_id = $2 AND tenant_id = $3",
                                new_stock, ingredient["ingredient_id"], tenant_id
                            )
                        else:
                            await conn.execute(
                                "INSERT INTO tenant_inventory (tenant_id, ingredient_id, current_stock, minimum_stock, last_updated) VALUES ($1, $2, $3, 0, NOW())",
                                tenant_id, ingredient["ingredient_id"], -quantity_to_deduct
                            )

                        await conn.execute(
                            """
                            INSERT INTO tenant_ingredient_movements (
                                tenant_id, ingredient_id, movement_type,
                                quantity_change, unit, previous_stock, new_stock,
                                reference_table, reference_id, reason, created_by, created_at
                            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW())
                            """,
                            tenant_id,
                            ingredient["ingredient_id"],
                            "consumption",
                            -quantity_to_deduct,
                            ingredient["unit"],
                            previous_stock,
                            new_stock,
                            "orders",
                            order_id,
                            f"Venta de {item['quantity']}x {item.get('product_name', '')} - Orden #{order_row['order_number']}",
                            user_id
                        )

                    await capture_order_item_ingredients(
                        conn,
                        order_item_id,
                        product_id,
                        float(item["quantity"]),
                        str(tenant_id),
                    )

                if split_payments:
                    from app.services.customer_wallet_service import apply_wallet_for_order

                    for payment in split_payments:
                        payment_method_for_row = payment["payment_method"]
                        payment_method_id_for_row = payment.get("payment_method_id")
                        payment_method_uuid_for_row = (
                            UUID(payment_method_id_for_row) if payment_method_id_for_row else None
                        )
                        payment_amount = round(float(payment["amount"]), 2)
                        payment_row = await conn.fetchrow(
                            """
                            INSERT INTO order_payments
                                (order_id, tenant_id, amount, payment_method, payment_method_id, created_by_user_id, cash_received)
                            VALUES ($1, $2, $3, $4, $5::uuid, $6::uuid, $7)
                            RETURNING id
                            """,
                            order_id,
                            tenant_id,
                            payment_amount,
                            payment_method_for_row,
                            str(payment_method_uuid_for_row) if payment_method_uuid_for_row else None,
                            str(user_id) if user_id else None,
                            payment.get("cash_received"),
                        )
                        if payment_method_for_row == "customer_wallet":
                            await apply_wallet_for_order(
                                conn,
                                customer_uuid,
                                tenant_id,
                                Decimal(str(payment_amount)),
                                order_id,
                                user_id,
                                payment_row["id"],
                            )
                elif payment_method == "customer_wallet" and customer_uuid:
                    from app.services.customer_wallet_service import apply_wallet_for_order

                    await apply_wallet_for_order(
                        conn,
                        customer_uuid,
                        tenant_id,
                        Decimal(str(total_amount)),
                        order_id,
                        user_id,
                    )

                gl_order_date = local_date_for_tenant(order_datetime, timezone_name)
                gl_payment_method_id = payment_method_uuid

                try:
                    tax_config = await _get_tenant_tax_config(conn, tenant_id)
                    await _post_order_gl_entry(
                        conn=conn,
                        tenant_id=tenant_id,
                        order_id=order_id,
                        order_date=gl_order_date,
                        total_amount=Decimal(str(total_amount)),
                        payment_method=payment_method,
                        payment_method_id=gl_payment_method_id,
                        tax_config=tax_config,
                        order_number=int(order_row["order_number"]),
                        payment_splits=split_payments or None,
                    )
                except MissingAccountRoleError:
                    raise
                except Exception as e:
                    logger.error(f"GL entry failed for manual order {order_id}: {e}")

                try:
                    await _post_order_cogs_gl_entry(
                        conn=conn,
                        tenant_id=tenant_id,
                        order_id=order_id,
                        order_date=gl_order_date,
                        order_number=int(order_row["order_number"]),
                    )
                except MissingAccountRoleError:
                    raise
                except Exception as e:
                    logger.error(f"COGS GL entry failed for manual order {order_id}: {e}")

        # Award waros for completed manual order (fire-and-forget — never blocks)
        if customer_id:
            try:
                asyncio.create_task(
                    evaluate_and_award(order_row["id"], UUID(customer_id), tenant_id)
                )
            except Exception as _waros_err:
                logger.warning(f"Could not schedule waros evaluation: {_waros_err}")

        return {
            "success": True,
            "data": {
                "id": str(order_row["id"]),
                "order_number": int(order_row["order_number"]),
                "order_date": order_row["order_date"].isoformat(),
                "total_amount": float(total_amount),
                "status": "completed",
                "payment_method": payment_method,
                "payment_method_id": str(payment_method_uuid) if payment_method_uuid else None,
                "payment_status": payment_status,
                "discount_type": normalized_discount_type,
                "discount_value": normalized_discount_value,
                "discount_amount": float(discount_amount),
            }
        }

    except (AuthenticationError, APIError):
        raise
    except Exception as e:
        logger.error(f"Error creating manual order: {str(e)}")
        raise APIError(f"Error creating manual order: {str(e)}", status_code=500)


async def get_products_sold(
    request: Request,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    category_id: Optional[str] = None,
    sort: str = "qty_desc",
    search: Optional[str] = None,
    channel: Optional[str] = None,
) -> dict:
    """
    Get products sold aggregated by product, filtered by date range and category.
    Only includes orders with status = 'completed'.
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            timezone_name = await resolve_tenant_timezone(conn, tenant_id)
            where_conditions = ["o.tenant_id = $1", "o.status = 'completed'"]
            params: List = [tenant_id]
            param_count = 1

            parsed_date_from = parse_date(date_from)
            parsed_date_to = parse_date(date_to)
            param_count = _append_local_date_bounds(
                where_conditions,
                params,
                param_count,
                "o.order_date",
                parsed_date_from,
                parsed_date_to,
                timezone_name,
            )

            if category_id:
                param_count += 1
                where_conditions.append(f"p.category_id = ${param_count}::uuid")
                params.append(category_id)

            if search and search.strip():
                param_count += 1
                where_conditions.append(f"p.name ILIKE ${param_count}")
                params.append(f"%{search.strip()}%")

            # channel ∈ {'online', 'mesa', 'pos'} — same as get_orders_list
            if channel == 'online':
                where_conditions.append("o.online_cart_id IS NOT NULL")
            elif channel == 'mesa':
                where_conditions.append("o.table_session_id IS NOT NULL")
            elif channel == 'pos':
                where_conditions.append("o.pos_cart_id IS NOT NULL AND o.table_session_id IS NULL")

            where_clause = " AND ".join(where_conditions)

            sort_map = {
                "qty_desc": "quantity_sold DESC",
                "revenue_desc": "total_revenue DESC",
                "name_asc": "product_name ASC",
            }
            order_by = sort_map.get(sort, "quantity_sold DESC")

            query = f"""
                SELECT
                    p.id::text AS product_id,
                    p.name AS product_name,
                    p.category_id::text AS category_id,
                    c.name AS category_name,
                    SUM(oi.quantity)::int AS quantity_sold,
                    SUM(oi.subtotal) AS total_revenue
                FROM order_items oi
                JOIN orders o ON o.id = oi.order_id
                JOIN product p ON p.id = oi.product_id
                LEFT JOIN categories c ON c.id = p.category_id
                WHERE {where_clause}
                GROUP BY p.id, p.name, p.category_id, c.name
                ORDER BY {order_by}
            """

            rows = await conn.fetch(query, *params)

            data = [
                {
                    "product_id": row["product_id"],
                    "product_name": row["product_name"],
                    "category_id": row["category_id"],
                    "category_name": row["category_name"],
                    "quantity_sold": row["quantity_sold"],
                    "total_revenue": float(row["total_revenue"]),
                }
                for row in rows
            ]

            total_qty = sum(r["quantity_sold"] for r in data)
            total_revenue = sum(r["total_revenue"] for r in data)

            return {
                "success": True,
                "data": data,
                "totals": {
                    "quantity_sold": total_qty,
                    "total_revenue": total_revenue,
                },
            }

    except (AuthenticationError, APIError):
        raise
    except Exception as e:
        logger.error(f"Error getting products sold: {str(e)}")
        raise APIError(f"Error getting products sold: {str(e)}", status_code=500)


async def send_invoice_email(
    request: Request,
    order_id: UUID,
    recipient_email: str,
) -> Dict[str, Any]:
    """
    Send the WARO-branded receipt/invoice email for an order (warocol.com#603, #1769).

    Loads the order header + items + optional invoice + tenant business profile
    from DB, then dispatches `send_pos_receipt_email` (SES + template).

    When an accepted electronic invoice exists, includes invoice fields and
    optional PDF/XML attachments. Otherwise sends a receipt-style email so
    cashiers can resend from `/ventas/[id]` without DIAN acceptance.

    Raises:
        HTTPException 404 — order not found for the session tenant
        HTTPException 502 — SES rejected the send
    """
    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    async with get_db_connection() as conn:
        # 1. Order header (also enforces tenant ownership).
        order_row = await conn.fetchrow(
            """SELECT o.id, o.order_number, o.order_date, o.total_amount, o.payment_method,
                      o.payment_status, o.discount_amount,
                      c.name AS customer_name,
                      c.email AS customer_email,
                      c.fiscal_id_type AS customer_fiscal_id_type,
                      c.fiscal_id AS customer_fiscal_id,
                      c.fiscal_business_name AS customer_fiscal_business_name,
                      c.fiscal_email AS customer_fiscal_email
               FROM orders o
               LEFT JOIN profile c ON c.id = o.customer_id
               WHERE o.id = $1 AND o.tenant_id = $2""",
            order_id, tenant_id,
        )
        if not order_row:
            raise HTTPException(status_code=404, detail="Order not found")

        # 2. Invoice header — optional. Accepted invoices get FE attachments;
        # missing / non-accepted still allow a receipt-style resend (#1769).
        invoice_row = await conn.fetchrow(
            """SELECT prefix, invoice_number, cufe, status, r2_pdf_key, r2_xml_key,
                      emitted_at, created_at
               FROM electronic_invoices
               WHERE order_id = $1 AND tenant_id = $2
               ORDER BY created_at DESC
               LIMIT 1""",
            order_id, tenant_id,
        )
        include_invoice = bool(invoice_row and invoice_row['status'] == 'accepted')

        # 3. Tax breakdown — net line base matches GL / cierre_service.
        tax_config = await _get_tenant_tax_config(conn, tenant_id)
        items_for_tax = await conn.fetch(
            """SELECT COALESCE(p.tax_category, 'standard') AS tax_category,
                      COALESCE(oi.net_total, oi.subtotal, 0) AS subtotal
               FROM order_items oi
               JOIN product p ON p.id = oi.product_id
               WHERE oi.order_id = $1""",
            order_id,
        )
        std_tax, liq_tax, tax_label = _compute_tax_breakdown(items_for_tax, tax_config)
        promo_summary = await _get_order_promo_summary(conn, order_id)
        waro_summary = await _get_order_waro_redemption_summary(conn, order_id)

        # 4. Items in the shape `pos_receipt_template` expects.
        item_rows = await conn.fetch(
            """SELECT oi.id, oi.quantity, oi.subtotal,
                      p.name as product_name
               FROM order_items oi
               LEFT JOIN product p ON p.id = oi.product_id
               WHERE oi.order_id = $1
               ORDER BY oi.created_at""",
            order_id,
        )
        items = []
        for it in item_rows:
            modifiers_rows = await conn.fetch(
                """SELECT modifier_name, price_at_purchase
                   FROM order_item_modifiers WHERE order_item_id = $1""",
                it['id'],
            )
            items.append({
                'quantity': it['quantity'],
                'subtotal': float(it['subtotal']),
                'product': {'name': it['product_name'] or 'Producto'},
                'modifiers': [
                    {'name': m['modifier_name'], 'price': float(m['price_at_purchase'])}
                    for m in modifiers_rows
                ],
            })

        # 5. Business profile + fiscal issuer (tenant is DIAN emisor; not WARO brand).
        profile_row = await conn.fetchrow(
            """SELECT COALESCE(p.display_name, t.name) AS display_name,
                      p.address, p.city, p.phone_number,
                      fd.business_name AS fiscal_business_name,
                      fd.business_name AS business_name,
                      fd.nit, fd.fiscal_address, fd.city AS fiscal_city,
                      fd.phone AS fiscal_phone, fd.email AS fiscal_email,
                      fd.matias_company_id
               FROM tenants t
               LEFT JOIN tenant_public_profiles p ON p.tenant_id = t.id
               LEFT JOIN tenant_fiscal_data fd ON fd.tenant_id = t.id
               WHERE t.id = $1""",
            tenant_id,
        )

        resolution_row = None
        if include_invoice:
            resolution_row = await conn.fetchrow(
                """SELECT resolution_number, prefix, date_from, date_to,
                          from_number, to_number
                   FROM dian_resolutions
                   WHERE tenant_id = $1
                     AND prefix = $2
                     AND is_active = true
                   ORDER BY created_at DESC
                   LIMIT 1""",
                tenant_id, invoice_row['prefix'],
            )

    discount_amount = float(order_row['discount_amount']) if order_row['discount_amount'] is not None else 0.0
    promo_savings = float(promo_summary["promo_savings"])
    promo_breakdown = promo_summary["promo_breakdown"]
    waro_discount_cop = float(waro_summary["waro_discount_cop"])
    # Subtotal: gross list total when manual discount, promos, or WaRo need a reference line.
    subtotal_for_email = (
        sum(float(it['subtotal']) for it in item_rows)
        if discount_amount > 0 or promo_savings > 0 or waro_discount_cop > 0
        else 0.0
    )

    tax_details = _tax_detail_rows(items_for_tax, std_tax, liq_tax, tax_label, tax_config)
    invoice_presentation = None
    if include_invoice:
        invoice_presentation = _build_invoice_presentation(
            invoice_row,
            order_row,
            profile_row,
            resolution_row,
            tax_details,
            serialize_datetimes=False,
            provider="matias",
        )

    # FE email: subject/header prefer tenant fiscal name (emisor), not product brand.
    issuer_name = (invoice_presentation or {}).get("issuer", {}).get("name") if invoice_presentation else None
    email_business_name = commercial_header_name(
        fiscal_row=profile_row,
        public_profile=profile_row,
        prefer_fiscal=True,
    ) or issuer_name or (_row_get(profile_row, "display_name") if profile_row else None)
    email_address = (
        ((invoice_presentation or {}).get("issuer") or {}).get("address")
        if invoice_presentation else None
    ) or (_row_get(profile_row, "address") if profile_row else None)
    email_city = (
        ((invoice_presentation or {}).get("issuer") or {}).get("city")
        if invoice_presentation else None
    ) or (_row_get(profile_row, "city") if profile_row else None)
    email_phone = (
        ((invoice_presentation or {}).get("issuer") or {}).get("phone")
        if invoice_presentation else None
    ) or (_row_get(profile_row, "phone_number") if profile_row else None)

    # api-warolabs#657: persist the attempt before SES. Raw token goes only
    # into the pixel URL; the DB stores its SHA-256 hash. Fail-open: when
    # tracking persistence is unavailable, send exactly as before #657.
    tracking_token = invoice_email_tracking_service.generate_tracking_token()
    tracking_token_hash = invoice_email_tracking_service.hash_tracking_token(tracking_token)
    delivery_id = await invoice_email_tracking_service.create_pending_delivery(
        tenant_id=tenant_id,
        order_id=order_id,
        recipient_email=recipient_email,
        tracking_token_hash=tracking_token_hash,
    )
    pixel_url = (
        invoice_email_tracking_service.build_pixel_url(tracking_token)
        if delivery_id is not None
        else None
    )

    send_result = await send_pos_receipt_email(
        customer_email=recipient_email,
        order_number=int(order_row['order_number']),
        total_amount=float(order_row['total_amount']),
        payment_method=order_row['payment_method'] or '',
        items=items,
        order_date=order_row['order_date'],
        tenant_id=str(tenant_id),
        business_name=email_business_name,
        business_address=email_address,
        business_city=email_city,
        business_phone=email_phone,
        discount_amount=discount_amount,
        subtotal=subtotal_for_email,
        standard_tax=std_tax,
        liquor_tax=liq_tax,
        standard_tax_label=tax_label,
        promo_savings=promo_savings,
        promo_breakdown=promo_breakdown,
        waro_redemption_summary=waro_summary,
        invoice_prefix=invoice_row['prefix'] if include_invoice else None,
        invoice_number=int(invoice_row['invoice_number']) if include_invoice else None,
        invoice_cufe=invoice_row['cufe'] if include_invoice else None,
        invoice_presentation=invoice_presentation,
        return_details=True,
        tracking_pixel_url=pixel_url,
    )
    if isinstance(send_result, dict):
        success = bool(send_result.get("success"))
        attachment_status = send_result.get("attachments") or (
            _invoice_attachment_flags(invoice_row) if include_invoice else {"pdf": False, "xml": False}
        )
        attachment_warnings = send_result.get("attachment_warnings") or []
    else:
        success = bool(send_result)
        attachment_status = (
            _invoice_attachment_flags(invoice_row) if include_invoice else {"pdf": False, "xml": False}
        )
        attachment_warnings = []

    if not success:
        if delivery_id is not None:
            await invoice_email_tracking_service.mark_delivery_failed(
                delivery_id, failure_code="ses_rejected"
            )
        raise HTTPException(
            status_code=502,
            detail="No se pudo enviar el correo. Intentá nuevamente en unos segundos.",
        )

    if delivery_id is not None:
        await invoice_email_tracking_service.mark_delivery_sent(delivery_id)

    return {
        'success': True,
        'sent_to': recipient_email,
        'attachments': attachment_status,
        'attachment_warnings': attachment_warnings,
    }

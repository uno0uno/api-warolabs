"""
Cierre Contable Service
Preview (Cierre X), create close (Cierre Z), list, and detail.

Issue: https://github.com/uno0uno/warocol.com/issues/311
"""
import logging
from typing import Any, Dict, List, Optional
from uuid import UUID
from datetime import date, datetime
from fastapi import Request
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError, APIError
from app.models.cierre import CierreCreate

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared — aggregation queries
# ---------------------------------------------------------------------------

def _build_order_date_filter(
    period_start: date,
    period_end: date,
    period_start_time: Optional[datetime],
    period_end_time: Optional[datetime],
    param_offset: int = 2,
):
    """
    Returns (sql_fragment, [p_start, p_end]) for order_date filtering.

    When exact timestamps are provided, compares directly against order_date
    (TIMESTAMPTZ).  Otherwise, truncates order_date to the Bogota calendar date
    before comparing (legacy behaviour).
    """
    p2 = f"${param_offset}"
    p3 = f"${param_offset + 1}"
    if period_start_time and period_end_time:
        sql = f"AND order_date >= {p2} AND order_date <= {p3}"
        return sql, [period_start_time, period_end_time]
    sql = (
        f"AND (order_date AT TIME ZONE 'America/Bogota')::date >= {p2} "
        f"AND (order_date AT TIME ZONE 'America/Bogota')::date <= {p3}"
    )
    return sql, [period_start, period_end]


async def _compute_preview(
    conn,
    tenant_id: UUID,
    period_start: date,
    period_end: date,
    completed_only: bool = False,
    period_start_time: Optional[datetime] = None,
    period_end_time: Optional[datetime] = None,
) -> dict:
    """
    Runs the three aggregation queries (sales, gastos, open tables) and returns
    a plain dict. Used by both get_cierre_preview and create_cierre.

    completed_only=True  → only 'completed' orders (used for the actual Cierre Z)
    completed_only=False → 'completed' + 'pending' (used for Cierre X preview so
                           open-table orders are visible)

    When period_start_time / period_end_time are supplied the order filter uses
    exact TIMESTAMPTZ comparison, enabling cross-midnight shift closing.
    """
    status_filter = "AND status = 'completed'" if completed_only else "AND status IN ('completed', 'pending')"
    date_filter, date_params = _build_order_date_filter(
        period_start, period_end, period_start_time, period_end_time
    )
    sales_row = await conn.fetchrow(
        f"""
        SELECT
            COALESCE(SUM(total_amount), 0)                                              AS total_sales,
            COALESCE(COUNT(*), 0)                                                       AS items_sold,
            COALESCE(SUM(total_amount) FILTER (WHERE payment_method = 'cash'),    0)    AS total_cash,
            COALESCE(SUM(total_amount) FILTER (WHERE payment_method = 'card'),    0)    AS total_card,
            COALESCE(SUM(total_amount) FILTER (WHERE payment_method = 'digital'), 0)    AS total_digital,
            COALESCE(SUM(total_amount) FILTER (WHERE payment_method = 'credit'),  0)    AS total_credit
        FROM orders
        WHERE tenant_id = $1
          {status_filter}
          {date_filter}
        """,
        tenant_id, *date_params,
    )

    gastos_row = await conn.fetchrow(
        """
        SELECT COALESCE(SUM(amount), 0) AS gastos_efectivo
        FROM tenant_expenses
        WHERE tenant_id = $1
          AND payment_method = 'cash'
          AND transaction_date >= $2
          AND transaction_date <= $3
        """,
        tenant_id, period_start, period_end,
    )

    open_tables_row = await conn.fetchrow(
        """
        SELECT COUNT(*) AS open_tables_count
        FROM table_sessions
        WHERE tenant_id = $1
          AND closed_at IS NULL
          AND opened_at::date >= $2
          AND opened_at::date <= $3
        """,
        tenant_id, period_start, period_end,
    )

    total_cash = float(sales_row["total_cash"])
    gastos_efectivo = float(gastos_row["gastos_efectivo"])
    cash_expected = total_cash - gastos_efectivo

    return {
        "totalSales":       float(sales_row["total_sales"]),
        "itemsSold":        int(sales_row["items_sold"]),
        "totalCash":        total_cash,
        "totalCard":        float(sales_row["total_card"]),
        "totalDigital":     float(sales_row["total_digital"]),
        "totalCredit":      float(sales_row["total_credit"]),
        "gastosEfectivo":   gastos_efectivo,
        "cashExpected":     cash_expected,
        "openTablesCount":  int(open_tables_row["open_tables_count"]),
    }


# ---------------------------------------------------------------------------
# Shared — payment breakdown computation
# ---------------------------------------------------------------------------

async def _compute_breakdown_rows(
    conn,
    tenant_id: UUID,
    period_start: date,
    period_end: date,
    completed_only: bool = False,
    period_start_time: Optional[datetime] = None,
    period_end_time: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """
    Compute per-method payment totals for the period via UNION ALL:
      - Modern orders (payment_method_id IS NOT NULL): join payment_methods + payment_method_groups
      - Legacy orders (payment_method_id IS NULL): group by payment_method VARCHAR slug

    Returns list of {group_slug, method_name, total}, excluding zero-total rows.
    When period_start_time / period_end_time are supplied, uses exact TIMESTAMPTZ comparison.
    """
    status_filter = "AND status = 'completed'" if completed_only else "AND status IN ('completed', 'pending')"
    date_filter, date_params = _build_order_date_filter(
        period_start, period_end, period_start_time, period_end_time
    )
    rows = await conn.fetch(
        f"""
        SELECT
            pmg.slug        AS group_slug,
            pm.name         AS method_name,
            COALESCE(SUM(o.total_amount), 0) AS total
        FROM orders o
        JOIN payment_methods pm ON pm.id = o.payment_method_id
        JOIN payment_method_groups pmg ON pmg.id = pm.group_id
        WHERE o.tenant_id = $1
          {status_filter}
          {date_filter}
          AND o.payment_method_id IS NOT NULL
        GROUP BY pmg.slug, pm.name

        UNION ALL

        SELECT
            o.payment_method AS group_slug,
            o.payment_method AS method_name,
            COALESCE(SUM(o.total_amount), 0) AS total
        FROM orders o
        WHERE o.tenant_id = $1
          {status_filter}
          {date_filter}
          AND o.payment_method_id IS NULL
          AND o.payment_method IS NOT NULL
        GROUP BY o.payment_method
        """,
        tenant_id, *date_params,
    )
    return [
        {
            "group_slug":  row["group_slug"],
            "method_name": row["method_name"],
            "total":       float(row["total"]),
        }
        for row in rows
        if float(row["total"]) > 0
    ]


# ---------------------------------------------------------------------------
# GET /cierre/preview
# ---------------------------------------------------------------------------

async def get_cierre_preview(
    request: Request,
    period_start: date,
    period_end: date,
    completed_only: bool = False,
    period_start_time: Optional[datetime] = None,
    period_end_time: Optional[datetime] = None,
) -> dict:
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection(use_transaction=False) as conn:
            preview = await _compute_preview(
                conn, tenant_id, period_start, period_end,
                completed_only=completed_only,
                period_start_time=period_start_time,
                period_end_time=period_end_time,
            )
            breakdown = await _compute_breakdown_rows(
                conn, tenant_id, period_start, period_end,
                completed_only=completed_only,
                period_start_time=period_start_time,
                period_end_time=period_end_time,
            )
            preview["breakdown"] = breakdown

        return {"success": True, "data": preview}

    except (AuthenticationError, APIError):
        raise
    except Exception as exc:
        logger.error(f"Error in get_cierre_preview: {exc}")
        raise APIError(f"Error in get_cierre_preview: {exc}", status_code=500)


# ---------------------------------------------------------------------------
# POST /cierre
# ---------------------------------------------------------------------------

async def create_cierre(request: Request, body: CierreCreate) -> dict:
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection(use_transaction=True) as conn:
            # 0. Validation: multi-day periods require exact timestamps
            if body.period_start != body.period_end and not (body.period_start_time and body.period_end_time):
                raise APIError(
                    "Para períodos de varios días debes especificar hora de inicio y fin exactas.",
                    status_code=422,
                )

            # 1. Unified overlap check using effective time windows.
            #    Date-only cierres (single day) are treated as 00:00–23:59:59 Bogotá.
            #    Timestamped cierres use their exact TIMESTAMPTZ values.
            #    This allows multiple non-overlapping shifts on the same day.
            from zoneinfo import ZoneInfo
            bog = ZoneInfo("America/Bogota")
            if body.period_start_time and body.period_end_time:
                eff_start = body.period_start_time
                eff_end   = body.period_end_time
            else:
                eff_start = datetime(
                    body.period_start.year, body.period_start.month, body.period_start.day,
                    0, 0, 0, tzinfo=bog,
                )
                eff_end = datetime(
                    body.period_end.year, body.period_end.month, body.period_end.day,
                    23, 59, 59, tzinfo=bog,
                )

            overlap = await conn.fetchrow(
                """
                SELECT id FROM accounting_period
                WHERE tenant_id = $1
                  AND NOT (
                    COALESCE(
                        period_end_time,
                        (period_end::timestamp + INTERVAL '23:59:59') AT TIME ZONE 'America/Bogota'
                    ) <= $2
                    OR
                    COALESCE(
                        period_start_time,
                        period_start::timestamp AT TIME ZONE 'America/Bogota'
                    ) >= $3
                  )
                """,
                tenant_id, eff_start, eff_end,
            )
            if overlap:
                raise APIError(
                    "Ya existe un cierre para este período o uno que se superpone.",
                    status_code=409,
                )

            # 2. Preview aggregation (completed only — cash already received)
            preview = await _compute_preview(
                conn, tenant_id, body.period_start, body.period_end,
                completed_only=True,
                period_start_time=body.period_start_time,
                period_end_time=body.period_end_time,
            )

            # 3. Open tables check — skip for past periods (mesas actuales no pertenecen al período)
            # Use Bogota date so the check is correct even when the server runs in UTC.
            from zoneinfo import ZoneInfo
            today_bogota = datetime.now(ZoneInfo("America/Bogota")).date()
            is_past_period = body.period_end < today_bogota
            if not is_past_period and preview["openTablesCount"] > 0:
                raise APIError(
                    f"Hay {preview['openTablesCount']} mesa(s) con cuenta abierta. "
                    "Cierra todas las mesas antes de registrar el cierre del día.",
                    status_code=409,
                )

            # 4. INSERT accounting_period
            period_row = await conn.fetchrow(
                """
                INSERT INTO accounting_period
                    (tenant_id, period_start, period_end, period_start_time, period_end_time)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id, closed_at
                """,
                tenant_id, body.period_start, body.period_end,
                body.period_start_time, body.period_end_time,
            )
            period_id = period_row["id"]
            closed_at = period_row["closed_at"]

            # 5. INSERT closing_summary
            cash_difference = body.cash_counted - preview["cashExpected"]
            summary_row = await conn.fetchrow(
                """
                INSERT INTO closing_summary (
                    accounting_period_id, tenant_id,
                    total_sales, items_sold,
                    total_cash, total_card, total_digital, total_credit,
                    gastos_efectivo, cash_expected, cash_counted, cash_difference,
                    notes
                ) VALUES (
                    $1, $2,
                    $3, $4,
                    $5, $6, $7, $8,
                    $9, $10, $11, $12,
                    $13
                )
                RETURNING id, created_at
                """,
                period_id, tenant_id,
                preview["totalSales"], preview["itemsSold"],
                preview["totalCash"], preview["totalCard"],
                preview["totalDigital"], preview["totalCredit"],
                preview["gastosEfectivo"], preview["cashExpected"],
                body.cash_counted, cash_difference,
                body.notes,
            )

            # 6. Compute and persist payment breakdown
            breakdown_rows = await _compute_breakdown_rows(
                conn, tenant_id, body.period_start, body.period_end,
                completed_only=True,
                period_start_time=body.period_start_time,
                period_end_time=body.period_end_time,
            )
            if breakdown_rows:
                await conn.execute(
                    """
                    INSERT INTO cierre_payment_breakdown (cierre_id, group_slug, method_name, total)
                    SELECT $1, unnest($2::text[]), unnest($3::text[]), unnest($4::numeric[])
                    """,
                    summary_row["id"],
                    [r["group_slug"] for r in breakdown_rows],
                    [r["method_name"] for r in breakdown_rows],
                    [r["total"] for r in breakdown_rows],
                )

        return {
            "success": True,
            "data": {
                "id":                   str(summary_row["id"]),
                "accountingPeriodId":   str(period_id),
                "tenantId":             str(tenant_id),
                "periodStart":          body.period_start.isoformat(),
                "periodEnd":            body.period_end.isoformat(),
                "periodStartTime":      body.period_start_time.isoformat() if body.period_start_time else None,
                "periodEndTime":        body.period_end_time.isoformat()   if body.period_end_time   else None,
                "totalSales":           preview["totalSales"],
                "itemsSold":            preview["itemsSold"],
                "totalCash":            preview["totalCash"],
                "totalCard":            preview["totalCard"],
                "totalDigital":         preview["totalDigital"],
                "totalCredit":          preview["totalCredit"],
                "gastosEfectivo":       preview["gastosEfectivo"],
                "cashExpected":         preview["cashExpected"],
                "cashCounted":          body.cash_counted,
                "cashDifference":       cash_difference,
                "notes":                body.notes,
                "closedAt":             closed_at.isoformat(),
                "breakdown":            breakdown_rows,
            },
        }

    except (AuthenticationError, APIError):
        raise
    except Exception as exc:
        logger.error(f"Error in create_cierre: {exc}")
        raise APIError(f"Error in create_cierre: {exc}", status_code=500)


# ---------------------------------------------------------------------------
# GET /cierre
# ---------------------------------------------------------------------------

async def list_cierres(
    request: Request,
    period_start: Optional[date] = None,
    period_end: Optional[date] = None,
) -> dict:
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        date_filter = ""
        params = [tenant_id]
        if period_start:
            params.append(period_start)
            date_filter += f" AND ap.period_start >= ${len(params)}"
        if period_end:
            params.append(period_end)
            date_filter += f" AND ap.period_end <= ${len(params)}"

        async with get_db_connection(use_transaction=False) as conn:
            rows = await conn.fetch(
                f"""
                SELECT
                    cs.id, cs.accounting_period_id, cs.tenant_id,
                    ap.period_start, ap.period_end, ap.period_start_time, ap.period_end_time, ap.closed_at,
                    cs.total_sales, cs.items_sold,
                    cs.total_cash, cs.total_card, cs.total_digital, cs.total_credit,
                    cs.gastos_efectivo, cs.cash_expected, cs.cash_counted, cs.cash_difference,
                    cs.notes
                FROM closing_summary cs
                JOIN accounting_period ap ON ap.id = cs.accounting_period_id
                WHERE cs.tenant_id = $1
                {date_filter}
                ORDER BY ap.period_start DESC
                """,
                *params,
            )

        data = [_row_to_dict(row) for row in rows]
        return {"success": True, "data": data}

    except (AuthenticationError, APIError):
        raise
    except Exception as exc:
        logger.error(f"Error in list_cierres: {exc}")
        raise APIError(f"Error in list_cierres: {exc}", status_code=500)


# ---------------------------------------------------------------------------
# GET /cierre/{cierre_id}
# ---------------------------------------------------------------------------

async def get_cierre(request: Request, cierre_id: UUID) -> dict:
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection(use_transaction=False) as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    cs.id, cs.accounting_period_id, cs.tenant_id,
                    ap.period_start, ap.period_end, ap.period_start_time, ap.period_end_time, ap.closed_at,
                    cs.total_sales, cs.items_sold,
                    cs.total_cash, cs.total_card, cs.total_digital, cs.total_credit,
                    cs.gastos_efectivo, cs.cash_expected, cs.cash_counted, cs.cash_difference,
                    cs.notes
                FROM closing_summary cs
                JOIN accounting_period ap ON ap.id = cs.accounting_period_id
                WHERE cs.id = $1 AND cs.tenant_id = $2
                """,
                cierre_id, tenant_id,
            )

            if not row:
                raise APIError("Cierre no encontrado", status_code=404)

            breakdown_rows = await conn.fetch(
                """
                SELECT group_slug, method_name, total
                FROM cierre_payment_breakdown
                WHERE cierre_id = $1
                ORDER BY group_slug, method_name
                """,
                row["id"],
            )
            breakdown = [
                {
                    "groupSlug":  r["group_slug"],
                    "methodName": r["method_name"],
                    "total":      float(r["total"]),
                }
                for r in breakdown_rows
            ]

        data = _row_to_dict(row)
        data["breakdown"] = breakdown
        return {"success": True, "data": data}

    except (AuthenticationError, APIError):
        raise
    except Exception as exc:
        logger.error(f"Error in get_cierre: {exc}")
        raise APIError(f"Error in get_cierre: {exc}", status_code=500)


# ---------------------------------------------------------------------------
# GET /cierre/mensual
# ---------------------------------------------------------------------------

async def get_cierre_mensual(request: Request, year: int, month: int) -> dict:
    import calendar
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        # First and last day of the requested month
        _, last_day = calendar.monthrange(year, month)
        period_start = date(year, month, 1)
        period_end   = date(year, month, last_day)

        async with get_db_connection(use_transaction=False) as conn:
            rows = await conn.fetch(
                """
                SELECT
                    cs.id, cs.accounting_period_id, cs.tenant_id,
                    ap.period_start, ap.period_end, ap.period_start_time, ap.period_end_time, ap.closed_at,
                    cs.total_sales, cs.items_sold,
                    cs.total_cash, cs.total_card, cs.total_digital, cs.total_credit,
                    cs.gastos_efectivo, cs.cash_expected, cs.cash_counted, cs.cash_difference,
                    cs.notes
                FROM closing_summary cs
                JOIN accounting_period ap ON ap.id = cs.accounting_period_id
                WHERE cs.tenant_id = $1
                  AND ap.period_start >= $2
                  AND ap.period_end   <= $3
                ORDER BY ap.period_start ASC
                """,
                tenant_id, period_start, period_end,
            )

        daily = [_row_to_dict(row) for row in rows]
        days_in_month = last_day

        totals = {
            "totalSales":     sum(r["totalSales"]     for r in daily),
            "itemsSold":      sum(r["itemsSold"]       for r in daily),
            "totalCash":      sum(r["totalCash"]       for r in daily),
            "totalCard":      sum(r["totalCard"]       for r in daily),
            "totalDigital":   sum(r["totalDigital"]    for r in daily),
            "totalCredit":    sum(r["totalCredit"]     for r in daily),
            "gastosEfectivo": sum(r["gastosEfectivo"]  for r in daily),
            "cashExpected":   sum(r["cashExpected"]    for r in daily),
            "cashCounted":    sum(r["cashCounted"]     for r in daily),
            "cashDifference": sum(r["cashDifference"]  for r in daily),
            "daysClosed":     len(daily),
            "daysInMonth":    days_in_month,
        }

        return {"success": True, "data": {"totals": totals, "daily": daily}}

    except (AuthenticationError, APIError):
        raise
    except Exception as exc:
        logger.error(f"Error in get_cierre_mensual: {exc}")
        raise APIError(f"Error in get_cierre_mensual: {exc}", status_code=500)


# ---------------------------------------------------------------------------
# GET /cierre/ultimo
# ---------------------------------------------------------------------------

async def get_ultimo_cierre(request: Request) -> dict:
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection(use_transaction=False) as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    cs.id, cs.accounting_period_id, cs.tenant_id,
                    ap.period_start, ap.period_end, ap.period_start_time, ap.period_end_time, ap.closed_at,
                    cs.total_sales, cs.items_sold,
                    cs.total_cash, cs.total_card, cs.total_digital, cs.total_credit,
                    cs.gastos_efectivo, cs.cash_expected, cs.cash_counted, cs.cash_difference,
                    cs.notes
                FROM closing_summary cs
                JOIN accounting_period ap ON ap.id = cs.accounting_period_id
                WHERE cs.tenant_id = $1
                ORDER BY ap.period_end DESC, ap.closed_at DESC
                LIMIT 1
                """,
                tenant_id,
            )

        if not row:
            return {"success": True, "data": None}

        return {"success": True, "data": _row_to_dict(row)}

    except (AuthenticationError, APIError):
        raise
    except Exception as exc:
        logger.error(f"Error in get_ultimo_cierre: {exc}")
        raise APIError(f"Error in get_ultimo_cierre: {exc}", status_code=500)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _row_to_dict(row) -> dict:
    return {
        "id":                   str(row["id"]),
        "accountingPeriodId":   str(row["accounting_period_id"]),
        "tenantId":             str(row["tenant_id"]),
        "periodStart":          row["period_start"].isoformat(),
        "periodEnd":            row["period_end"].isoformat(),
        "periodStartTime":      row["period_start_time"].isoformat() if row["period_start_time"] else None,
        "periodEndTime":        row["period_end_time"].isoformat()   if row["period_end_time"]   else None,
        "totalSales":           float(row["total_sales"]),
        "itemsSold":            int(row["items_sold"]),
        "totalCash":            float(row["total_cash"]),
        "totalCard":            float(row["total_card"]),
        "totalDigital":         float(row["total_digital"]),
        "totalCredit":          float(row["total_credit"]),
        "gastosEfectivo":       float(row["gastos_efectivo"]),
        "cashExpected":         float(row["cash_expected"]),
        "cashCounted":          float(row["cash_counted"]),
        "cashDifference":       float(row["cash_difference"]),
        "notes":                row["notes"],
        "closedAt":             row["closed_at"].isoformat(),
    }

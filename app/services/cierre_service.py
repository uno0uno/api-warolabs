"""
Cierre Contable Service
Preview (Cierre X), create close (Cierre Z), list, and detail.

Issue: https://github.com/uno0uno/warocol.com/issues/311
"""
import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional
from uuid import UUID
from datetime import date, datetime
from fastapi import Request, Response
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError, APIError
from app.models.cierre import CierreCreate

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GL helpers — Auto-posting ventas/arqueo → GL (#378)
# ---------------------------------------------------------------------------

# Payment slug → PUC debit account code
_SLUG_DEBIT_CODE: Dict[str, str] = {
    "cash":    "1105",   # Caja general
    "digital": "1110",   # Bancos (Nequi, Daviplata)
    "card":    "1110",   # Bancos
    "credit":  "1305",   # Clientes (fiado — accounts receivable)
}

INGRESOS_CODE   = "4135"   # Comercio al por menor
COGS_CODE       = "6135"   # Costo de ventas
INVENTARIO_CODE = "1435"   # Inventarios — materia prima y suministros


async def _get_tenant_tax_config(conn, tenant_id: UUID) -> Dict[str, Any]:
    """
    Return tax config for the tenant.  Falls back to all-disabled defaults
    if no row exists (safe for tenants created before migration 027).
    """
    row = await conn.fetchrow(
        """SELECT inc_applicable, inc_rate,   inc_gl_account_code,
                  inc_included_in_price,
                  liquor_tax_applicable, liquor_tax_rate, liquor_tax_gl_account_code,
                  iva_applicable, iva_rate, iva_gl_account_code,
                  iva_included_in_price
           FROM tenant_tax_config WHERE tenant_id = $1""",
        tenant_id,
    )
    if row:
        return dict(row)
    return {
        "inc_applicable":             False,
        "inc_rate":                   Decimal("0.0800"),
        "inc_gl_account_code":        "2495",
        "inc_included_in_price":      True,
        "liquor_tax_applicable":      False,
        "liquor_tax_rate":            Decimal("0.0000"),
        "liquor_tax_gl_account_code": "2408",
        "iva_applicable":             False,
        "iva_rate":                   Decimal("0.1900"),
        "iva_gl_account_code":        "2408",
        "iva_included_in_price":      False,
    }


async def _post_cierre_gl_entry(
    conn,
    tenant_id: UUID,
    summary_id: UUID,
    period_date: date,
    breakdown_rows: List[Dict],
    tax_config: Dict[str, Any],
) -> None:
    """
    Post a multi-line GL entry for a cierre (arqueo / ventas).

    Debit lines : one per payment slug that has a non-zero total.
    Credit lines: split between 4135 (net income) and 2408 (tax payable)
                  if INC or IVA is enabled for this tenant; otherwise a single
                  credit to 4135 for the full amount.

    Silently skips if: total_sales is zero, any required account is missing,
    or the period is already closed.
    Caller MUST wrap in try/except for graceful degrade.
    """
    # Aggregate totals per payment slug from breakdown
    slug_totals: Dict[str, Decimal] = {}
    for row in breakdown_rows:
        slug = row.get("group_slug", "")
        total = Decimal(str(row.get("total", 0)))
        if total > 0:
            slug_totals[slug] = slug_totals.get(slug, Decimal("0")) + total

    total_sales = sum(slug_totals.values())
    if total_sales <= 0:
        logger.info(f"[GL] Cierre {summary_id}: zero sales — skip GL post")
        return

    # Check period open
    closed = await conn.fetchval(
        """SELECT 1 FROM tenant_monthly_periods
           WHERE tenant_id = $1 AND year = $2 AND month = $3 AND status = 'closed'""",
        tenant_id, period_date.year, period_date.month,
    )
    if closed:
        logger.warning(
            f"[GL] Period {period_date.year}-{period_date.month:02d} closed — "
            f"skip GL post for cierre {summary_id}"
        )
        return

    # Resolve debit account UUIDs
    debit_accounts: Dict[str, Any] = {}
    for slug, amount in slug_totals.items():
        code = _SLUG_DEBIT_CODE.get(slug)
        if not code:
            logger.warning(f"[GL] Unknown payment slug '{slug}' — skip debit line")
            continue
        acct = await conn.fetchrow(
            "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = $2 AND is_active = true",
            tenant_id, code,
        )
        if not acct:
            logger.warning(
                f"[GL] Debit account {code} not found for tenant {tenant_id} — "
                f"skip GL post for cierre {summary_id}"
            )
            return
        debit_accounts[slug] = {"id": acct["id"], "code": code, "amount": amount}

    # Resolve credit account(s)
    ingresos_acct = await conn.fetchrow(
        "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = $2 AND is_active = true",
        tenant_id, INGRESOS_CODE,
    )
    if not ingresos_acct:
        logger.warning(
            f"[GL] Ingresos account {INGRESOS_CODE} not found for tenant {tenant_id} — "
            f"skip GL post for cierre {summary_id}"
        )
        return

    # Determine tax split
    tax_amount = Decimal("0")
    tax_acct_id = None
    if tax_config.get("inc_applicable"):
        rate = Decimal(str(tax_config["inc_rate"]))
        tax_amount = total_sales - (total_sales / (1 + rate))
        tax_code = str(tax_config["inc_gl_account_code"])
        tax_row = await conn.fetchrow(
            "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = $2 AND is_active = true",
            tenant_id, tax_code,
        )
        if tax_row:
            tax_acct_id = tax_row["id"]
    elif tax_config.get("iva_applicable"):
        rate = Decimal(str(tax_config["iva_rate"]))
        tax_amount = total_sales - (total_sales / (1 + rate))
        tax_code = str(tax_config["iva_gl_account_code"])
        tax_row = await conn.fetchrow(
            "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = $2 AND is_active = true",
            tenant_id, tax_code,
        )
        if tax_row:
            tax_acct_id = tax_row["id"]

    net_income = total_sales - tax_amount
    ts = float(total_sales)

    description = f"Cierre {period_date.isoformat()} — ventas"

    async with conn.transaction():
        entry_row = await conn.fetchrow(
            """INSERT INTO tenant_journal_entries
                   (tenant_id, entry_date, period_year, period_month,
                    description, source_module, source_id, status,
                    total_debit, total_credit, posted_at)
               VALUES ($1, $2, $3, $4, $5, 'ventas', $6, 'posted', $7, $8, NOW())
               RETURNING id""",
            tenant_id, period_date, period_date.year, period_date.month,
            description, summary_id, ts, ts,
        )
        entry_id = entry_row["id"]

        # Debit lines — one per payment method
        line_order = 0
        for slug, info in debit_accounts.items():
            await conn.execute(
                """INSERT INTO tenant_journal_lines
                       (journal_entry_id, account_id, debit, credit, description, line_order)
                   VALUES ($1, $2, $3, 0, $4, $5)""",
                entry_id, info["id"], float(info["amount"]),
                f"{description} ({slug})", line_order,
            )
            line_order += 1

        # Credit line — net income to 4135
        await conn.execute(
            """INSERT INTO tenant_journal_lines
                   (journal_entry_id, account_id, debit, credit, description, line_order)
               VALUES ($1, $2, 0, $3, $4, $5)""",
            entry_id, ingresos_acct["id"], float(net_income),
            f"{description} — ingreso neto", line_order,
        )
        line_order += 1

        # Credit line — tax payable (if applicable and account resolved)
        if tax_amount > 0 and tax_acct_id:
            await conn.execute(
                """INSERT INTO tenant_journal_lines
                       (journal_entry_id, account_id, debit, credit, description, line_order)
                   VALUES ($1, $2, 0, $3, $4, $5)""",
                entry_id, tax_acct_id, float(tax_amount),
                f"{description} — impuesto", line_order,
            )

    logger.info(
        f"[GL] ✅ Posted cierre entry {entry_id} for summary {summary_id} "
        f"(total={ts}, net={float(net_income)}, tax={float(tax_amount)})"
    )


async def _post_order_gl_entry(
    conn,
    tenant_id: UUID,
    order_id: UUID,
    order_date: date,
    total_amount: Decimal,
    payment_method: str,
    payment_method_id: Optional[UUID],
    tax_config: Dict[str, Any],
) -> None:
    """
    Post a double-entry GL journal entry for a single completed order (POS or domicilio).

    source_module = 'orden' (distinguishable from cierre 'ventas' entries)
    source_id     = order_id

    DR  [payment account]    debit_total   (1105 Caja / 1110 Bancos / 1305 Clientes)
    CR  4135 Ingresos        net_revenue
    CR  2495/2408 Impuesto   tax_amount    (only if tax enabled and account resolved)

    Tax modes (per tenant_tax_config):
      inc_included_in_price=True  (default): price already includes tax — extract formula
      inc_included_in_price=False           : tax added on top — additive formula

    Idempotent: skips if an 'orden' entry already exists for this order_id.
    Caller MUST wrap in try/except — GL failure must never roll back the order.
    """
    # ── Idempotency guard ──────────────────────────────────────────────────
    existing = await conn.fetchval(
        """SELECT id FROM tenant_journal_entries
           WHERE source_module = 'orden' AND source_id = $1 AND tenant_id = $2""",
        order_id, tenant_id,
    )
    if existing:
        logger.info(f"[GL] Order {order_id}: entry already exists — skip (idempotent)")
        return

    if total_amount <= 0:
        logger.info(f"[GL] Order {order_id}: zero amount — skip GL post")
        return

    # ── Resolve debit account: specific method → group → slug fallback ────
    debit_code = None
    if payment_method_id:
        pm_row = await conn.fetchrow(
            """SELECT COALESCE(pm.gl_account_code, pmg.gl_account_code) AS code
               FROM payment_methods pm
               JOIN payment_method_groups pmg ON pm.group_id = pmg.id
               WHERE pm.id = $1""",
            payment_method_id,
        )
        if pm_row and pm_row["code"]:
            debit_code = pm_row["code"]
    if not debit_code:
        debit_code = _SLUG_DEBIT_CODE.get(payment_method or "", "1105")

    debit_acct = await conn.fetchrow(
        "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = $2 AND is_active = true",
        tenant_id, debit_code,
    )
    if not debit_acct:
        logger.warning(
            f"[GL] Debit account {debit_code} not found for tenant {tenant_id} — "
            f"skip GL post for order {order_id}"
        )
        return

    # ── Resolve 4135 Ingresos ──────────────────────────────────────────────
    ingresos_acct = await conn.fetchrow(
        "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = $2 AND is_active = true",
        tenant_id, INGRESOS_CODE,
    )
    if not ingresos_acct:
        logger.warning(
            f"[GL] Ingresos account {INGRESOS_CODE} not found for tenant {tenant_id} — "
            f"skip GL post for order {order_id}"
        )
        return

    # ── Tax calculation — two modes ────────────────────────────────────────
    tax_amount = Decimal("0")
    tax_acct_id = None
    debit_total = total_amount   # may increase in additive mode

    if tax_config.get("inc_applicable"):
        rate = Decimal(str(tax_config["inc_rate"]))
        tax_code = str(tax_config["inc_gl_account_code"])
        if tax_config.get("inc_included_in_price", True):
            # Extractive: price already contains the tax
            tax_amount = total_amount - (total_amount / (1 + rate))
            net_revenue = total_amount / (1 + rate)
        else:
            # Additive: tax is on top of the base price
            tax_amount = total_amount * rate
            net_revenue = total_amount
            debit_total = total_amount + tax_amount
        tax_row = await conn.fetchrow(
            "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = $2 AND is_active = true",
            tenant_id, tax_code,
        )
        if tax_row:
            tax_acct_id = tax_row["id"]
    elif tax_config.get("iva_applicable"):
        rate = Decimal(str(tax_config["iva_rate"]))
        tax_code = str(tax_config["iva_gl_account_code"])
        if tax_config.get("iva_included_in_price", False):
            tax_amount = total_amount - (total_amount / (1 + rate))
            net_revenue = total_amount / (1 + rate)
        else:
            tax_amount = total_amount * rate
            net_revenue = total_amount
            debit_total = total_amount + tax_amount
        tax_row = await conn.fetchrow(
            "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = $2 AND is_active = true",
            tenant_id, tax_code,
        )
        if tax_row:
            tax_acct_id = tax_row["id"]
    else:
        net_revenue = total_amount

    dt = float(debit_total)
    description = f"Venta {order_date.isoformat()} — orden {order_id}"

    # ── Insert entry + lines (savepoint if inside outer transaction) ───────
    async with conn.transaction():
        entry_row = await conn.fetchrow(
            """INSERT INTO tenant_journal_entries
                   (tenant_id, entry_date, period_year, period_month,
                    description, source_module, source_id, status,
                    total_debit, total_credit, posted_at)
               VALUES ($1, $2, $3, $4, $5, 'orden', $6, 'posted', $7, $8, NOW())
               RETURNING id""",
            tenant_id, order_date, order_date.year, order_date.month,
            description, order_id, dt, dt,
        )
        entry_id = entry_row["id"]

        # Debit line — payment account
        await conn.execute(
            """INSERT INTO tenant_journal_lines
                   (journal_entry_id, account_id, debit, credit, description, line_order)
               VALUES ($1, $2, $3, 0, $4, 0)""",
            entry_id, debit_acct["id"], dt, description,
        )

        # Credit line — net revenue to 4135
        await conn.execute(
            """INSERT INTO tenant_journal_lines
                   (journal_entry_id, account_id, debit, credit, description, line_order)
               VALUES ($1, $2, 0, $3, $4, 1)""",
            entry_id, ingresos_acct["id"], float(net_revenue),
            f"{description} — ingreso neto",
        )

        # Credit line — tax payable (if applicable and account resolved)
        if tax_amount > 0 and tax_acct_id:
            await conn.execute(
                """INSERT INTO tenant_journal_lines
                       (journal_entry_id, account_id, debit, credit, description, line_order)
                   VALUES ($1, $2, 0, $3, $4, 2)""",
                entry_id, tax_acct_id, float(tax_amount),
                f"{description} — impuesto",
            )

    logger.info(
        f"[GL] ✅ Posted order entry {entry_id} for order {order_id} "
        f"(total={dt}, net={float(net_revenue)}, tax={float(tax_amount)})"
    )


async def _post_order_cogs_gl_entry(
    conn,
    tenant_id: UUID,
    order_id: UUID,
    order_date: date,
) -> None:
    """
    Post a COGS GL journal entry for a completed order.

    DR  6135 Costo de ventas      total_ingredient_cost
    CR  1435 Inventarios          total_ingredient_cost

    Cost basis: sum of order_item_ingredients.total_cost (captured at sale time
    using last purchase unit_cost × quantity consumed).

    Rules:
    - Only posts if total ingredient cost > 0 (skip if no purchase history)
    - Idempotent: skips if source_module='orden_cogs' entry already exists
    - Missing 6135 or 1435 account → warning logged, no exception raised
    - Failure must never block order completion — caller wraps in try/except
    """
    # ── Idempotency guard ──────────────────────────────────────────────────
    existing = await conn.fetchval(
        """SELECT id FROM tenant_journal_entries
           WHERE source_module = 'orden_cogs' AND source_id = $1 AND tenant_id = $2""",
        order_id, tenant_id,
    )
    if existing:
        logger.info(f"[GL] COGS Order {order_id}: entry already exists — skip (idempotent)")
        return

    # ── Sum ingredient cost from order_item_ingredients ───────────────────
    total_cogs = await conn.fetchval(
        """SELECT COALESCE(SUM(oii.total_cost), 0)
           FROM order_item_ingredients oii
           JOIN order_items oi ON oi.id = oii.order_item_id
           WHERE oi.order_id = $1
             AND oii.total_cost IS NOT NULL
             AND oii.total_cost > 0""",
        order_id,
    )
    if not total_cogs or float(total_cogs) <= 0:
        logger.info(f"[GL] Order {order_id}: no ingredient cost data — skip COGS entry")
        return

    # ── Resolve 6135 Costo de ventas ───────────────────────────────────────
    cogs_acct = await conn.fetchrow(
        "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = $2 AND is_active = true",
        tenant_id, COGS_CODE,
    )
    if not cogs_acct:
        logger.warning(
            f"[GL] COGS account {COGS_CODE} not found for tenant {tenant_id} — "
            f"skip COGS entry for order {order_id}"
        )
        return

    # ── Resolve 1435 Inventarios ───────────────────────────────────────────
    inv_acct = await conn.fetchrow(
        "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = $2 AND is_active = true",
        tenant_id, INVENTARIO_CODE,
    )
    if not inv_acct:
        logger.warning(
            f"[GL] Inventory account {INVENTARIO_CODE} not found for tenant {tenant_id} — "
            f"skip COGS entry for order {order_id}"
        )
        return

    # ── Insert entry + 2 lines ─────────────────────────────────────────────
    amount = float(total_cogs)
    description = f"CMV {order_date.isoformat()} — orden {order_id}"

    async with conn.transaction():
        entry_row = await conn.fetchrow(
            """INSERT INTO tenant_journal_entries
                   (tenant_id, entry_date, period_year, period_month,
                    description, source_module, source_id, status,
                    total_debit, total_credit, posted_at)
               VALUES ($1, $2, $3, $4, $5, 'orden_cogs', $6, 'posted', $7, $8, NOW())
               RETURNING id""",
            tenant_id, order_date, order_date.year, order_date.month,
            description, order_id, amount, amount,
        )
        entry_id = entry_row["id"]

        # Debit — 6135 Costo de ventas
        await conn.execute(
            """INSERT INTO tenant_journal_lines
                   (journal_entry_id, account_id, debit, credit, description, line_order)
               VALUES ($1, $2, $3, 0, $4, 0)""",
            entry_id, cogs_acct["id"], amount, description,
        )

        # Credit — 1435 Inventarios
        await conn.execute(
            """INSERT INTO tenant_journal_lines
                   (journal_entry_id, account_id, debit, credit, description, line_order)
               VALUES ($1, $2, 0, $3, $4, 1)""",
            entry_id, inv_acct["id"], amount, description,
        )

    logger.info(
        f"[GL] ✅ Posted COGS entry {entry_id} for order {order_id} (cogs={amount})"
    )


async def _void_cierre_gl_entry(
    conn,
    tenant_id: UUID,
    summary_id: UUID,
    reason: str = "Cierre eliminado",
) -> None:
    """
    Find and void the most recent posted ventas GL entry for this cierre.
    Silently skips if no entry found (pre-#378 cierre) or period is closed.
    Caller MUST wrap in try/except for graceful degrade.
    """
    entry = await conn.fetchrow(
        """SELECT id, entry_date, period_year, period_month, description,
                  total_debit, total_credit
           FROM tenant_journal_entries
           WHERE tenant_id = $1 AND source_module = 'ventas' AND source_id = $2
                 AND status = 'posted'
           ORDER BY created_at DESC
           LIMIT 1""",
        tenant_id, summary_id,
    )
    if not entry:
        logger.info(f"[GL] No posted GL entry for cierre {summary_id} — skip void")
        return

    closed = await conn.fetchval(
        """SELECT 1 FROM tenant_monthly_periods
           WHERE tenant_id = $1 AND year = $2 AND month = $3 AND status = 'closed'""",
        tenant_id, entry["period_year"], entry["period_month"],
    )
    if closed:
        logger.warning(
            f"[GL] Period {entry['period_year']}-{entry['period_month']:02d} closed — "
            f"skip GL void for cierre {summary_id}"
        )
        return

    original_lines = await conn.fetch(
        """SELECT account_id, debit, credit, description, line_order
           FROM tenant_journal_lines
           WHERE journal_entry_id = $1 ORDER BY line_order""",
        entry["id"],
    )

    async with conn.transaction():
        await conn.execute(
            "UPDATE tenant_journal_entries SET status = 'voided', voided_at = NOW() WHERE id = $1",
            entry["id"],
        )
        rev_row = await conn.fetchrow(
            """INSERT INTO tenant_journal_entries
                   (tenant_id, entry_date, period_year, period_month,
                    description, source_module, source_id, status,
                    total_debit, total_credit, posted_at)
               VALUES ($1, $2, $3, $4, $5, 'system', $6, 'posted', $7, $8, NOW())
               RETURNING id""",
            tenant_id, entry["entry_date"], entry["period_year"], entry["period_month"],
            f"Reversión: {entry['description']} — {reason}",
            entry["id"],
            float(entry["total_debit"]), float(entry["total_credit"]),
        )
        rev_id = rev_row["id"]
        for line in original_lines:
            await conn.execute(
                """INSERT INTO tenant_journal_lines
                       (journal_entry_id, account_id, debit, credit, description, line_order)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                rev_id, line["account_id"],
                float(line["credit"]), float(line["debit"]),
                line["description"], line["line_order"],
            )

    logger.info(
        f"[GL] ✅ Voided cierre GL entry {entry['id']} → reversing {rev_id} "
        f"for cierre {summary_id}"
    )


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
            COALESCE(SUM(total_amount), 0)  AS total_sales,
            COALESCE(COUNT(*), 0)           AS items_sold
        FROM orders
        WHERE tenant_id = $1
          {status_filter}
          {date_filter}
        """,
        tenant_id, *date_params,
    )

    # Payment method totals — COALESCE split vs legacy:
    # Split orders: sum from order_payments rows
    # Legacy orders (no order_payments rows): use orders.total_amount + orders.payment_method
    method_rows = await conn.fetch(
        f"""
        SELECT
            op.payment_method AS method,
            COALESCE(SUM(op.amount), 0) AS total
        FROM order_payments op
        JOIN orders o ON o.id = op.order_id
        WHERE o.tenant_id = $1
          {status_filter.replace('status', 'o.status')}
          {date_filter.replace('order_date', 'o.order_date')}
        GROUP BY op.payment_method

        UNION ALL

        SELECT
            payment_method AS method,
            COALESCE(SUM(total_amount), 0) AS total
        FROM orders
        WHERE tenant_id = $1
          {status_filter}
          {date_filter}
          AND NOT EXISTS (SELECT 1 FROM order_payments op WHERE op.order_id = orders.id)
          AND payment_method IS NOT NULL
        GROUP BY payment_method
        """,
        tenant_id, *date_params,
    )

    # Aggregate method totals in Python to handle UNION ALL correctly
    method_totals: Dict[str, float] = {}
    for row in method_rows:
        m = row["method"]
        if m:
            method_totals[m] = method_totals.get(m, 0.0) + float(row["total"])

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
        FROM table_sessions ts
        JOIN tables t ON t.id = ts.table_id
        WHERE ts.tenant_id = $1
          AND ts.closed_at IS NULL
          AND ts.is_discarded = FALSE
          AND ts.opened_at::date >= $2
          AND ts.opened_at::date <= $3
          AND t.is_bar IS FALSE
        """,
        tenant_id, period_start, period_end,
    )

    total_cash = method_totals.get("cash", 0.0)
    gastos_efectivo = float(gastos_row["gastos_efectivo"])
    cash_expected = total_cash - gastos_efectivo

    return {
        "totalSales":       float(sales_row["total_sales"]),
        "itemsSold":        int(sales_row["items_sold"]),
        "totalCash":        total_cash,
        "totalCard":        method_totals.get("card", 0.0),
        "totalDigital":     method_totals.get("digital", 0.0),
        "totalCredit":      method_totals.get("credit", 0.0),
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
        -- Split orders: read from order_payments (with FK method → group)
        SELECT
            COALESCE(pmg.slug, op.payment_method)  AS group_slug,
            COALESCE(pm.name, op.payment_method)   AS method_name,
            COALESCE(SUM(op.amount), 0)             AS total
        FROM order_payments op
        JOIN orders o ON o.id = op.order_id
        LEFT JOIN payment_methods pm ON pm.id = op.payment_method_id
        LEFT JOIN payment_method_groups pmg ON pmg.id = pm.group_id
        WHERE o.tenant_id = $1
          {status_filter.replace('status', 'o.status')}
          {date_filter.replace('order_date', 'o.order_date')}
        GROUP BY COALESCE(pmg.slug, op.payment_method), COALESCE(pm.name, op.payment_method)

        UNION ALL

        -- Legacy orders with FK method (no order_payments rows)
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
          AND NOT EXISTS (SELECT 1 FROM order_payments op WHERE op.order_id = o.id)
        GROUP BY pmg.slug, pm.name

        UNION ALL

        -- Legacy orders with VARCHAR method only (no order_payments rows)
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
          AND NOT EXISTS (SELECT 1 FROM order_payments op WHERE op.order_id = o.id)
        GROUP BY o.payment_method
        """,
        tenant_id, *date_params,
    )
    # Aggregate across UNION ALL branches — same group_slug+method_name can appear
    # from both the order_payments branch and a legacy branch.
    aggregated: Dict[tuple, Dict[str, Any]] = {}
    for row in rows:
        key = (row["group_slug"], row["method_name"])
        total = float(row["total"])
        if key not in aggregated:
            aggregated[key] = {
                "group_slug":  row["group_slug"],
                "method_name": row["method_name"],
                "total":       total,
            }
        else:
            aggregated[key]["total"] += total
    return [r for r in aggregated.values() if r["total"] > 0]


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
                  AND deleted_at IS NULL
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

            # 7. GL auto-post — ventas/arqueo → GL (#378)
            #    SAVEPOINT: GL failure never rolls back the cierre save.
            try:
                async with conn.transaction():
                    tax_cfg = await _get_tenant_tax_config(conn, tenant_id)
                    await _post_cierre_gl_entry(
                        conn, tenant_id, summary_row["id"],
                        body.period_start, breakdown_rows or [], tax_cfg,
                    )
            except Exception as _gl_err:
                logger.warning(
                    f"[GL] cierre GL post failed for {summary_row['id']}: {_gl_err}"
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
                  AND ap.deleted_at IS NULL
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
                WHERE cs.id = $1 AND cs.tenant_id = $2 AND ap.deleted_at IS NULL
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
# DELETE /cierre/{cierre_id}  — soft delete
# ---------------------------------------------------------------------------

async def delete_cierre(request: Request, cierre_id: UUID) -> dict:
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection(use_transaction=True) as conn:
            row = await conn.fetchrow(
                """
                SELECT cs.id AS summary_id, ap.id AS ap_id
                FROM closing_summary cs
                JOIN accounting_period ap ON ap.id = cs.accounting_period_id
                WHERE cs.id = $1 AND cs.tenant_id = $2 AND ap.deleted_at IS NULL
                """,
                cierre_id, tenant_id,
            )
            if not row:
                raise APIError("Cierre no encontrado", status_code=404)

            # GL void — SAVEPOINT: GL failure never blocks the cierre delete.
            try:
                async with conn.transaction():
                    await _void_cierre_gl_entry(
                        conn, tenant_id, row["summary_id"], reason="Cierre eliminado"
                    )
            except Exception as _gl_err:
                logger.warning(
                    f"[GL] cierre GL void failed for {row['summary_id']}: {_gl_err}"
                )

            await conn.execute(
                "UPDATE accounting_period SET deleted_at = NOW() WHERE id = $1 AND tenant_id = $2",
                row["ap_id"], tenant_id,
            )

        return {"success": True, "data": None}

    except (AuthenticationError, APIError):
        raise
    except Exception as exc:
        logger.error(f"Error in delete_cierre: {exc}")
        raise APIError(f"Error in delete_cierre: {exc}", status_code=500)


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


# ---------------------------------------------------------------------------
# Monthly Accounting Period — #362
# ---------------------------------------------------------------------------

def _monthly_period_to_dict(row) -> dict:
    return {
        "id":         str(row["id"]),
        "tenantId":   str(row["tenant_id"]),
        "year":       row["year"],
        "month":      row["month"],
        "status":     row["status"],
        "closedBy":   str(row["closed_by"]) if row["closed_by"] else None,
        "closedAt":   row["closed_at"].isoformat() if row["closed_at"] else None,
        "notes":      row["notes"],
        "createdAt":  row["created_at"].isoformat(),
    }


async def get_monthly_period(request: Request, response: Response, year: int, month: int) -> dict:
    """Get or create a monthly period record for the given year/month."""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection(use_transaction=True) as conn:
            row = await conn.fetchrow(
                """
                SELECT id, tenant_id, year, month, status, closed_by, closed_at, notes, created_at
                FROM tenant_monthly_periods
                WHERE tenant_id = $1 AND year = $2 AND month = $3
                """,
                tenant_id, year, month,
            )
            if not row:
                row = await conn.fetchrow(
                    """
                    INSERT INTO tenant_monthly_periods (tenant_id, year, month, status)
                    VALUES ($1, $2, $3, 'open')
                    RETURNING id, tenant_id, year, month, status, closed_by, closed_at, notes, created_at
                    """,
                    tenant_id, year, month,
                )

        return {"success": True, "data": _monthly_period_to_dict(row)}

    except (AuthenticationError, APIError):
        raise
    except Exception as exc:
        logger.error(f"Error in get_monthly_period: {exc}")
        raise APIError(f"Error in get_monthly_period: {exc}", status_code=500)


async def close_monthly_period(
    request: Request,
    response: Response,
    year: int,
    month: int,
    notes: Optional[str] = None,
) -> dict:
    """Close a monthly accounting period. Raises 409 if already closed."""
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection(use_transaction=True) as conn:
            existing = await conn.fetchrow(
                """
                SELECT id, status FROM tenant_monthly_periods
                WHERE tenant_id = $1 AND year = $2 AND month = $3
                """,
                tenant_id, year, month,
            )

            if existing and existing["status"] == "closed":
                raise APIError(
                    f"El período {year}-{month:02d} ya está cerrado.",
                    status_code=409,
                )

            if existing:
                row = await conn.fetchrow(
                    """
                    UPDATE tenant_monthly_periods
                    SET status = 'closed', closed_by = $4, closed_at = NOW(), notes = $5
                    WHERE tenant_id = $1 AND year = $2 AND month = $3
                    RETURNING id, tenant_id, year, month, status, closed_by, closed_at, notes, created_at
                    """,
                    tenant_id, year, month, user_id, notes,
                )
            else:
                row = await conn.fetchrow(
                    """
                    INSERT INTO tenant_monthly_periods
                        (tenant_id, year, month, status, closed_by, closed_at, notes)
                    VALUES ($1, $2, $3, 'closed', $4, NOW(), $5)
                    RETURNING id, tenant_id, year, month, status, closed_by, closed_at, notes, created_at
                    """,
                    tenant_id, year, month, user_id, notes,
                )

        return {"success": True, "data": _monthly_period_to_dict(row)}

    except (AuthenticationError, APIError):
        raise
    except Exception as exc:
        logger.error(f"Error in close_monthly_period: {exc}")
        raise APIError(f"Error in close_monthly_period: {exc}", status_code=500)


async def assert_order_not_in_closed_monthly_period(conn, tenant_id, order_date) -> None:
    """
    Raises APIError(409) if the given order_date falls in a closed monthly period.
    This is the guard used by all order mutation functions.
    order_date can be a date, datetime, or date string 'YYYY-MM-DD'.
    """
    if order_date is None:
        return

    # Extract year and month from order_date
    if isinstance(order_date, str):
        # Parse 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM:SS' strings
        try:
            d = datetime.fromisoformat(order_date)
        except ValueError:
            return
        year = d.year
        month = d.month
    elif hasattr(order_date, "year") and hasattr(order_date, "month"):
        year = order_date.year
        month = order_date.month
    else:
        return

    row = await conn.fetchrow(
        """
        SELECT id FROM tenant_monthly_periods
        WHERE tenant_id = $1 AND year = $2 AND month = $3 AND status = 'closed'
        """,
        tenant_id, year, month,
    )
    if row:
        raise APIError(
            "Este pedido pertenece a un período contable cerrado. "
            "Contacta a tu contador para realizar correcciones.",
            status_code=409,
        )

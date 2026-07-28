"""
Cierre Contable Service
Preview (Cierre X), create close (Cierre Z), list, and detail.

Issue: https://github.com/uno0uno/warocol.com/issues/311
"""
import logging
import json
from decimal import Decimal
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from uuid import UUID
from datetime import date, datetime
from fastapi import Request, Response
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError, APIError
from app.core.timezones import (
    DEFAULT_TENANT_TIMEZONE,
    get_zoneinfo,
    resolve_tenant_timezone,
    tenant_today,
)
from app.models.cierre import (
    CierreCashSettingsUpdate,
    CierreCreate,
    CierreReconciliationReportedUpdate,
    CierreReconciliationResolve,
    OpenShiftCreate,
)
from app.services.billing_service import check_plan_quota_growth, check_plan_quota_period
from app.services.tip_tax_service import tip_settlement_total
from app.services.account_role_service import (
    AccountRole,
    resolve_account,
    resolve_payment_account,
    resolve_tax_account,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GL helpers — Auto-posting ventas/arqueo → GL (#378)
# ---------------------------------------------------------------------------

NON_RECONCILABLE_PAYMENT_GROUPS = {"cash", "untracked"}
RECONCILIATION_STATUSES = {"not_required", "pending", "matched", "needs_review", "resolved"}
RECONCILIATION_REASONS = {
    "commission",
    "timing",
    "missing_sale",
    "duplicate",
    "client_balance",
    "method_misclassified",
    "real_surplus",
    "real_shortage",
    "other",
}


def _initial_reconciliation_status(group_slug: str, total: float) -> str:
    if group_slug in NON_RECONCILABLE_PAYMENT_GROUPS or float(total or 0) == 0:
        return "not_required"
    return "pending"


def _status_from_reported(expected: Decimal, reported: Decimal) -> str:
    return "matched" if reported == expected else "needs_review"


def _normalize_reported_for_expected(expected: Decimal, reported: Decimal) -> Decimal:
    """Treat a positive typed amount as an outflow when the conciliable net is negative."""
    if expected < 0 and reported > 0:
        return -reported
    return reported


def _tip_gl_amounts(
    tip_amount: Decimal,
    tip_tax_amount: Decimal,
    tax_config: Dict[str, Any],
) -> tuple:
    """
    Return (settlement_debit, net_tip_revenue, tip_tax_credit) for GL posting.
    Mirrors additive vs extractive tip tax from tenant_tax_config.
    """
    from app.services.hospitality_tax_engine import tip_tax_is_additive

    tip_amt = Decimal(str(tip_amount or 0))
    tip_tax = Decimal(str(tip_tax_amount or 0))
    if tip_amt <= 0 and tip_tax <= 0:
        return Decimal("0"), Decimal("0"), Decimal("0")

    tip_tax_additive = tip_tax > 0 and tip_tax_is_additive(tax_config)

    if tip_tax_additive:
        settlement = tip_amt + tip_tax
        net_tip_revenue = tip_amt
    else:
        settlement = tip_amt
        net_tip_revenue = tip_amt - tip_tax
    return settlement, net_tip_revenue, tip_tax


async def _resolve_standard_tax_account_id(
    conn,
    tenant_id: UUID,
    tax_config: Dict[str, Any],
) -> Optional[UUID]:
    """Resolve INC/IVA credit account for standard (non-liquor) tax, including tip tax."""
    from app.services.hospitality_tax_engine import primary_gl_role

    tax_kind = primary_gl_role(tax_config)
    if not tax_kind:
        return None
    account = await resolve_tax_account(
        conn, tenant_id, tax_config, tax_kind, required=True
    )
    return account.id if account else None


async def _get_tenant_tax_config(conn, tenant_id: UUID) -> Dict[str, Any]:
    """
    Return tax config for the tenant.  Falls back to all-disabled defaults
    if no row exists (safe for tenants created before migration 027).
    """
    row = await conn.fetchrow(
        """SELECT inc_applicable, inc_rate, inc_gl_account_code, inc_gl_account_id,
                  inc_included_in_price,
                  liquor_tax_applicable, liquor_tax_rate,
                  liquor_tax_gl_account_code, liquor_tax_gl_account_id,
                  iva_applicable, iva_rate, iva_gl_account_code, iva_gl_account_id,
                  iva_included_in_price,
                  tax_lines, category_map, commercial_tax_applicable
           FROM tenant_tax_config WHERE tenant_id = $1""",
        tenant_id,
    )
    if row:
        return dict(row)
    return {
        "inc_applicable":             False,
        "inc_rate":                   Decimal("0.0800"),
        "inc_gl_account_code":        None,
        "inc_gl_account_id":          None,
        "inc_included_in_price":      True,
        "liquor_tax_applicable":      False,
        "liquor_tax_rate":            Decimal("0.0000"),
        "liquor_tax_gl_account_code": None,
        "liquor_tax_gl_account_id":   None,
        "iva_applicable":             False,
        "iva_rate":                   Decimal("0.1900"),
        "iva_gl_account_code":        None,
        "iva_gl_account_id":          None,
        "iva_included_in_price":      False,
        "tax_lines":                  None,
        "category_map":               None,
        "commercial_tax_applicable":  False,
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

    # Resolve every account before creating the journal.
    debit_accounts: Dict[str, Any] = {}
    for slug, amount in slug_totals.items():
        account = await resolve_payment_account(
            conn, tenant_id, slug, source="cierre"
        )
        debit_accounts[slug] = {
            "id": account.id,
            "code": account.code,
            "amount": amount,
        }

    revenue_account = await resolve_account(
        conn, tenant_id, AccountRole.SALES_REVENUE, source="cierre"
    )

    # Determine tax split (primary standard line; extractive on total_sales)
    from app.services.hospitality_tax_engine import resolve_tax_profile, tax_amount_decimal

    tax_amount = Decimal("0")
    tax_acct_id = None
    primary = resolve_tax_profile(tax_config).primary_line()
    if primary and total_sales > 0:
        # Cierre summary historically treats the period total as extractive on the
        # primary rate (included-in-price style), matching pre-engine INC/IVA posts.
        extractive_line = primary
        if not primary.included_in_price:
            # Keep legacy cierre behavior: always extract from total_sales.
            from dataclasses import replace

            extractive_line = replace(primary, included_in_price=True)
        tax_amount, _ = tax_amount_decimal(total_sales, extractive_line)
        tax_account = await resolve_tax_account(
            conn, tenant_id, tax_config, primary.gl_role,
        )
        tax_acct_id = tax_account.id if tax_account else None

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
            entry_id, revenue_account.id, float(net_income),
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
    order_number: Optional[int] = None,
    tip_amount: Decimal = Decimal("0"),
    tip_tax_amount: Decimal = Decimal("0"),
    advance_amount: Decimal = Decimal("0"),
    payment_splits: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """
    Post a double-entry GL journal entry for a single completed order (POS or domicilio).

    source_module = 'orden' (distinguishable from cierre 'ventas' entries)
    source_id     = order_id

    DR  [payment account]    debit_total
    CR  SALES_REVENUE        product net + dedicated tip line when tip > 0
    CR  2495/2408 Impuesto   tax_amount    (product + tip tax when applicable)

    Tax modes (per tenant_tax_config):
      inc_included_in_price=True  (default): price already includes tax — extract formula
      inc_included_in_price=False           : tax added on top — additive formula

    Tips: single-payment checkout posts product net on `— ingreso neto` and tip net on
    `— propina` (#915). Split flows defer tip to _post_deferred_order_tip_gl (#912).

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

    debit_account = await resolve_payment_account(
        conn,
        tenant_id,
        payment_method,
        payment_method_id=payment_method_id,
        source="order",
    )
    split_debits: List[Dict[str, Any]] = []
    if payment_splits:
        split_total = sum(Decimal(str(split.get("amount") or 0)) for split in payment_splits)
        if split_total > 0:
            for split in payment_splits:
                split_method_id = split.get("payment_method_id")
                if split_method_id and not isinstance(split_method_id, UUID):
                    split_method_id = UUID(str(split_method_id))
                split_debits.append(
                    {
                        "amount": Decimal(str(split.get("amount") or 0)),
                        "payment_method": split.get("payment_method") or "",
                        "account": await resolve_payment_account(
                            conn,
                            tenant_id,
                            split.get("payment_method") or "",
                            payment_method_id=split_method_id,
                            source="order_split",
                        ),
                    }
                )

    revenue_account = await resolve_account(
        conn, tenant_id, AccountRole.SALES_REVENUE, source="order"
    )

    # ── Fetch order items with per-product tax_category ──────────────────
    # Use net_total (post-discount) when available, falling back to subtotal.
    # This ensures INC/IVA is calculated on the amount actually charged to the
    # customer, not the pre-discount gross price (NIIF 15 para. 47 — transaction
    # price is net of trade discounts; Art. 454 ET for IVA; consistent for INC).
    order_items = await conn.fetch(
        """SELECT
               COALESCE(oi.net_total, oi.subtotal, 0) AS subtotal,
               COALESCE(p.tax_category, pv_p.tax_category, 'standard') AS tax_category
           FROM order_items oi
           LEFT JOIN product p ON p.id = oi.product_id
           LEFT JOIN product_variants pv ON pv.id = oi.variant_id
           LEFT JOIN product pv_p ON pv_p.id = pv.product_id
           WHERE oi.order_id = $1""",
        order_id,
    )

    # ── Accumulate subtotals per tax category ─────────────────────────────
    standard_subtotal = Decimal("0")
    liquor_subtotal   = Decimal("0")
    for item in order_items:
        cat = item["tax_category"] or "standard"
        sub = Decimal(str(item["subtotal"]))
        if cat == "liquor":
            liquor_subtotal += sub
        elif cat == "exempt":
            pass  # $0 tax contribution
        else:  # standard (or unknown — fall back to standard)
            standard_subtotal += sub
    # No items at all → treat total as standard (backwards-compatible fallback)
    if not order_items:
        standard_subtotal = total_amount

    # ── Calculate taxes per category (profile-driven tax_lines) ───────────
    from app.services.hospitality_tax_engine import compute_gl_category_taxes

    tax_result = compute_gl_category_taxes(
        standard_subtotal, liquor_subtotal, tax_config,
    )
    standard_tax = tax_result["standard_tax"]
    liquor_tax = tax_result["liquor_tax"]
    standard_is_additive = tax_result["standard_is_additive"]
    standard_acct_id = None
    liquor_acct_id = None

    if tax_result["standard_gl_role"] and standard_tax > 0:
        tax_account = await resolve_tax_account(
            conn, tenant_id, tax_config, tax_result["standard_gl_role"],
        )
        standard_acct_id = tax_account.id if tax_account else None

    if tax_result["liquor_gl_role"] and liquor_tax > 0:
        tax_account = await resolve_tax_account(
            conn, tenant_id, tax_config, tax_result["liquor_gl_role"],
        )
        liquor_acct_id = tax_account.id if tax_account else None

    # ── Compute debit_total and net_revenue ───────────────────────────────
    # Additive taxes (non-included INC/IVA, liquor) increase what the customer pays.
    # Extractive taxes are already embedded in total_amount.
    additive_extra = (standard_tax if standard_is_additive else Decimal("0")) + liquor_tax
    debit_total    = total_amount + additive_extra
    net_revenue    = debit_total - standard_tax - liquor_tax
    # Invariant: DR debit_total = CR net_revenue + CR standard_tax + CR liquor_tax ✓

    tip_settlement, tip_net_revenue, tip_tax_credit = _tip_gl_amounts(
        tip_amount, tip_tax_amount, tax_config,
    )
    tip_tax_acct_id = None
    if tip_tax_credit > 0:
        tip_tax_acct_id = await _resolve_standard_tax_account_id(conn, tenant_id, tax_config)
    product_net_revenue = net_revenue
    if tip_settlement > 0:
        debit_total += tip_settlement

    advance_debit = min(
        Decimal(str(advance_amount or 0)).quantize(Decimal("0.01")),
        debit_total,
    )
    payment_debit = debit_total - advance_debit
    debit_acct = None
    split_debit_lines: List[Dict[str, Any]] = []
    if payment_debit > 0:
        if split_debits:
            split_total = sum(split["amount"] for split in split_debits)
            remaining_debit = payment_debit
            for idx, split in enumerate(split_debits):
                if split["amount"] <= 0:
                    continue
                if idx == len(split_debits) - 1:
                    debit_amount = remaining_debit
                else:
                    debit_amount = (payment_debit * split["amount"] / split_total).quantize(Decimal("0.01"))
                    remaining_debit -= debit_amount
                if debit_amount <= 0:
                    continue
                split_acct = split["account"]
                split_debit_lines.append(
                    {
                        "account_id": split_acct.id,
                        "amount": debit_amount,
                        "payment_method": split["payment_method"],
                    }
                )
        else:
            debit_acct = debit_account
    advance_acct = None
    if advance_debit > 0:
        advance_acct = await resolve_account(
            conn, tenant_id, AccountRole.CUSTOMER_ADVANCES, source="order_advance"
        )

    dt = float(debit_total)
    description = f"#{order_number}" if order_number else f"Venta {order_date.isoformat()} — orden {order_id}"
    tip_description = f"{description} — propina"

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

        line_order = 0
        if advance_debit > 0 and advance_acct:
            await conn.execute(
                """INSERT INTO tenant_journal_lines
                       (journal_entry_id, account_id, debit, credit, description, line_order)
                   VALUES ($1, $2, $3, 0, $4, $5)""",
                entry_id,
                advance_acct.id,
                float(advance_debit),
                f"{description} — aplicación anticipo mesa",
                line_order,
            )
            line_order += 1
        if split_debit_lines:
            for split in split_debit_lines:
                await conn.execute(
                    """INSERT INTO tenant_journal_lines
                           (journal_entry_id, account_id, debit, credit, description, line_order)
                       VALUES ($1, $2, $3, 0, $4, $5)""",
                    entry_id,
                    split["account_id"],
                    float(split["amount"]),
                    f"{description} — {split['payment_method']}",
                    line_order,
                )
                line_order += 1
        elif payment_debit > 0 and debit_acct:
            await conn.execute(
                """INSERT INTO tenant_journal_lines
                       (journal_entry_id, account_id, debit, credit, description, line_order)
                   VALUES ($1, $2, $3, 0, $4, $5)""",
                entry_id,
                debit_acct.id,
                float(payment_debit),
                description,
                line_order,
            )
            line_order += 1

        # Credit line — product net revenue
        await conn.execute(
            """INSERT INTO tenant_journal_lines
                   (journal_entry_id, account_id, debit, credit, description, line_order)
               VALUES ($1, $2, 0, $3, $4, $5)""",
            entry_id, revenue_account.id, float(product_net_revenue),
            f"{description} — ingreso neto",
            line_order,
        )
        line_order += 1

        # Credit lines — one per active tax type (INC/IVA standard + IVA licores)
        if standard_tax > 0 and standard_acct_id:
            await conn.execute(
                """INSERT INTO tenant_journal_lines
                       (journal_entry_id, account_id, debit, credit, description, line_order)
                   VALUES ($1, $2, 0, $3, $4, $5)""",
                entry_id, standard_acct_id, float(standard_tax),
                f"{description} — INC/IVA",
                line_order,
            )
            line_order += 1
        if liquor_tax > 0 and liquor_acct_id:
            await conn.execute(
                """INSERT INTO tenant_journal_lines
                       (journal_entry_id, account_id, debit, credit, description, line_order)
                   VALUES ($1, $2, 0, $3, $4, $5)""",
                entry_id, liquor_acct_id, float(liquor_tax),
                f"{description} — IVA licores",
                line_order,
            )
            line_order += 1
        if tip_net_revenue > 0:
            await conn.execute(
                """INSERT INTO tenant_journal_lines
                       (journal_entry_id, account_id, debit, credit, description, line_order)
                   VALUES ($1, $2, 0, $3, $4, $5)""",
                entry_id, revenue_account.id, float(tip_net_revenue),
                tip_description,
                line_order,
            )
            line_order += 1
        if tip_tax_credit > 0 and tip_tax_acct_id:
            await conn.execute(
                """INSERT INTO tenant_journal_lines
                       (journal_entry_id, account_id, debit, credit, description, line_order)
                   VALUES ($1, $2, 0, $3, $4, $5)""",
                entry_id, tip_tax_acct_id, float(tip_tax_credit),
                f"{tip_description} — INC/IVA",
                line_order,
            )

    logger.info(
        f"[GL] ✅ Posted order entry {entry_id} for order {order_id} "
        f"(total={dt}, product_net={float(product_net_revenue)}, "
        f"inc={float(standard_tax)}, liquor={float(liquor_tax)}, "
        f"tip={float(tip_settlement)})"
    )


async def _post_deferred_order_tip_gl(
    conn,
    tenant_id: UUID,
    order_id: UUID,
    tip_amount: Decimal,
    tip_tax_amount: Decimal,
    payment_method: str,
    payment_method_id: Optional[UUID],
    tax_config: Dict[str, Any],
    order_number: Optional[int] = None,
) -> None:
    """
    Append tip lines to an existing orden GL entry when split payment completes (#912).

    Single-payment tips are included in _post_order_gl_entry at checkout. Split POS/mesa
    defer tip until is_complete so follow-up payments can set or change the tip (#910).

    Idempotent: skips when a propina journal line already exists for this order.
    """
    tip_settlement, tip_net_revenue, tip_tax_credit = _tip_gl_amounts(
        tip_amount, tip_tax_amount, tax_config,
    )
    if tip_settlement <= 0:
        return

    existing_tip = await conn.fetchval(
        """SELECT 1 FROM tenant_journal_lines jl
           JOIN tenant_journal_entries je ON je.id = jl.journal_entry_id
           WHERE je.source_module = 'orden' AND je.source_id = $1 AND je.tenant_id = $2
             AND je.status = 'posted'
             AND jl.description ILIKE '%propina%'""",
        order_id, tenant_id,
    )
    if existing_tip:
        logger.info(f"[GL] Order {order_id}: tip already in journal — skip (idempotent)")
        return

    entry_row = await conn.fetchrow(
        """SELECT id, total_debit, total_credit, description
           FROM tenant_journal_entries
           WHERE source_module = 'orden' AND source_id = $1 AND tenant_id = $2
             AND status = 'posted'
           ORDER BY created_at DESC
           LIMIT 1""",
        order_id, tenant_id,
    )
    if not entry_row:
        logger.warning(
            f"[GL] Order {order_id}: no orden entry for deferred tip — skip"
        )
        return

    debit_acct = await resolve_payment_account(
        conn,
        tenant_id,
        payment_method,
        payment_method_id=payment_method_id,
        source="deferred_tip",
    )
    revenue_account = await resolve_account(
        conn, tenant_id, AccountRole.SALES_REVENUE, source="deferred_tip"
    )

    tip_tax_acct_id = None
    if tip_tax_credit > 0:
        tip_tax_acct_id = await _resolve_standard_tax_account_id(conn, tenant_id, tax_config)

    entry_id = entry_row["id"]
    base_description = entry_row["description"] or (
        f"#{order_number}" if order_number else f"orden {order_id}"
    )
    tip_description = f"{base_description} — propina"

    max_line = await conn.fetchval(
        "SELECT COALESCE(MAX(line_order), -1) FROM tenant_journal_lines WHERE journal_entry_id = $1",
        entry_id,
    )
    line_order = int(max_line) + 1
    settlement_f = float(tip_settlement)
    new_debit = float(entry_row["total_debit"]) + settlement_f
    new_credit = float(entry_row["total_credit"]) + settlement_f

    async with conn.transaction():
        await conn.execute(
            """UPDATE tenant_journal_entries
               SET total_debit = $2, total_credit = $3
               WHERE id = $1""",
            entry_id, new_debit, new_credit,
        )
        await conn.execute(
            """INSERT INTO tenant_journal_lines
                   (journal_entry_id, account_id, debit, credit, description, line_order)
               VALUES ($1, $2, $3, 0, $4, $5)""",
            entry_id, debit_acct.id, settlement_f, tip_description, line_order,
        )
        line_order += 1
        await conn.execute(
            """INSERT INTO tenant_journal_lines
                   (journal_entry_id, account_id, debit, credit, description, line_order)
               VALUES ($1, $2, 0, $3, $4, $5)""",
            entry_id, revenue_account.id, float(tip_net_revenue),
            f"{tip_description} — ingreso neto", line_order,
        )
        if tip_tax_credit > 0 and tip_tax_acct_id:
            line_order += 1
            await conn.execute(
                """INSERT INTO tenant_journal_lines
                       (journal_entry_id, account_id, debit, credit, description, line_order)
                   VALUES ($1, $2, 0, $3, $4, $5)""",
                entry_id, tip_tax_acct_id, float(tip_tax_credit),
                f"{tip_description} — INC/IVA", line_order,
            )

    logger.info(
        f"[GL] ✅ Appended deferred tip to entry {entry_id} for order {order_id} "
        f"(tip_settlement={settlement_f})"
    )


async def _post_order_cogs_gl_entry(
    conn,
    tenant_id: UUID,
    order_id: UUID,
    order_date: date,
    order_number: Optional[int] = None,
) -> None:
    """
    Post a COGS GL journal entry for a completed order.

    DR  COGS                       total_ingredient_cost
    CR  INVENTORY                  total_ingredient_cost

    Cost basis: sum of order_item_ingredients.total_cost (captured at sale time
    using last purchase unit_cost × quantity consumed).

    Rules:
    - Only posts if total ingredient cost > 0 (skip if no purchase history)
    - Idempotent: skips if source_module='orden_cogs' entry already exists
    - Missing COGS or INVENTORY role fails explicitly before journal insertion
    - The caller decides whether non-configuration failures block completion
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

    cogs_acct = await resolve_account(
        conn, tenant_id, AccountRole.COGS, source="order_cogs"
    )
    inv_acct = await resolve_account(
        conn, tenant_id, AccountRole.INVENTORY, source="order_cogs"
    )

    # ── Insert entry + 2 lines ─────────────────────────────────────────────
    amount = float(total_cogs)
    description = f"CMV #{order_number}" if order_number else f"CMV {order_date.isoformat()} — orden {order_id}"

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

        # Debit — cost of goods sold
        await conn.execute(
            """INSERT INTO tenant_journal_lines
                   (journal_entry_id, account_id, debit, credit, description, line_order)
               VALUES ($1, $2, $3, 0, $4, 0)""",
            entry_id, cogs_acct.id, amount, description,
        )

        # Credit — inventory
        await conn.execute(
            """INSERT INTO tenant_journal_lines
                   (journal_entry_id, account_id, debit, credit, description, line_order)
               VALUES ($1, $2, 0, $3, $4, 1)""",
            entry_id, inv_acct.id, amount, description,
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
    timezone_name: str = DEFAULT_TENANT_TIMEZONE,
    param_offset: int = 2,
):
    """
    Returns (sql_fragment, [p_start, p_end]) for order_date filtering.

    When exact timestamps are provided, compares directly against order_date
    (TIMESTAMPTZ). Otherwise, truncates order_date to the tenant calendar date.
    """
    if period_start_time and period_end_time:
        p2 = f"${param_offset}"
        p3 = f"${param_offset + 1}"
        sql = f"AND order_date >= {p2} AND order_date <= {p3}"
        return sql, [period_start_time, period_end_time]
    p_tz = f"${param_offset}"
    p2 = f"${param_offset + 1}"
    p3 = f"${param_offset + 2}"
    sql = (
        f"AND (order_date AT TIME ZONE {p_tz})::date >= {p2} "
        f"AND (order_date AT TIME ZONE {p_tz})::date <= {p3}"
    )
    return sql, [timezone_name, period_start, period_end]


def _build_expense_filter(
    period_start: date,
    period_end: date,
    period_start_time: Optional[datetime],
    period_end_time: Optional[datetime],
    param_offset: int = 2,
    table_alias: Optional[str] = None,
):
    """
    Cash expenses: date-only arqueos use transaction_date (business day).
    Shift windows use created_at (transaction_date has no time component).
    """
    p2 = f"${param_offset}"
    p3 = f"${param_offset + 1}"
    prefix = f"{table_alias}." if table_alias else ""
    if period_start_time and period_end_time:
        sql = f"AND {prefix}created_at >= {p2} AND {prefix}created_at <= {p3}"
        return sql, [period_start_time, period_end_time]
    sql = f"AND {prefix}transaction_date >= {p2} AND {prefix}transaction_date <= {p3}"
    return sql, [period_start, period_end]


def _build_purchase_payment_filter(
    period_start: date,
    period_end: date,
    period_start_time: Optional[datetime],
    period_end_time: Optional[datetime],
    timezone_name: str = DEFAULT_TENANT_TIMEZONE,
    param_offset: int = 2,
    table_alias: Optional[str] = None,
):
    """
    Direct purchases use the cash/bank movement date for arqueo control.
    Date-only windows compare in tenant-local date; shift windows compare the
    actual timestamp so cross-midnight turns stay accurate.
    """
    prefix = f"{table_alias}." if table_alias else ""
    movement_expr = f"COALESCE({prefix}payment_date, {prefix}paid_at, {prefix}purchase_date)"
    if period_start_time and period_end_time:
        p2 = f"${param_offset}"
        p3 = f"${param_offset + 1}"
        sql = f"AND {movement_expr} >= {p2} AND {movement_expr} <= {p3}"
        return sql, [period_start_time, period_end_time]
    p_tz = f"${param_offset}"
    p2 = f"${param_offset + 1}"
    p3 = f"${param_offset + 2}"
    sql = (
        f"AND ({movement_expr} AT TIME ZONE {p_tz})::date >= {p2} "
        f"AND ({movement_expr} AT TIME ZONE {p_tz})::date <= {p3}"
    )
    return sql, [timezone_name, period_start, period_end]


def _build_open_tables_filter(
    period_start: date,
    period_end: date,
    period_start_time: Optional[datetime],
    period_end_time: Optional[datetime],
    timezone_name: str = DEFAULT_TENANT_TIMEZONE,
    param_offset: int = 2,
):
    """
    Open tables (closed_at IS NULL applied by caller).

    Date-only: opened on a tenant calendar day in [period_start, period_end].
    Shift window: session started on or before shift end (still-open tables
    that began before the shift still block the close).
    """
    if period_start_time and period_end_time:
        p_end = f"${param_offset}"
        return f"AND ts.opened_at <= {p_end}", [period_end_time]
    p_tz = f"${param_offset}"
    p2 = f"${param_offset + 1}"
    p3 = f"${param_offset + 2}"
    sql = (
        f"AND (ts.opened_at AT TIME ZONE {p_tz})::date >= {p2} "
        f"AND (ts.opened_at AT TIME ZONE {p_tz})::date <= {p3}"
    )
    return sql, [timezone_name, period_start, period_end]


# ---------------------------------------------------------------------------
# Shift opening helpers (#920)
# ---------------------------------------------------------------------------

def _effective_period_bounds(
    period_start: date,
    period_end: date,
    period_start_time: Optional[datetime],
    period_end_time: Optional[datetime],
    timezone_name: str = DEFAULT_TENANT_TIMEZONE,
) -> tuple:
    """Calendar day → tenant-local 00:00–23:59:59 unless exact timestamps supplied."""
    zone = get_zoneinfo(timezone_name)
    if period_start_time and period_end_time:
        return period_start_time, period_end_time
    eff_start = datetime(
        period_start.year, period_start.month, period_start.day,
        0, 0, 0, tzinfo=zone,
    )
    eff_end = datetime(
        period_end.year, period_end.month, period_end.day,
        23, 59, 59, tzinfo=zone,
    )
    return eff_start, eff_end


def _requires_open_shift(
    shift_template_id: Optional[UUID],
    period_start_time: Optional[datetime],
    period_end_time: Optional[datetime],
) -> bool:
    """Template and custom timestamp windows require a declared fondo de caja."""
    if shift_template_id:
        return True
    if period_start_time and period_end_time:
        return True
    return False


def _is_day_only_cierre_request(
    shift_template_id: Optional[UUID],
    period_start_time: Optional[datetime],
    period_end_time: Optional[datetime],
) -> bool:
    return not shift_template_id and not period_start_time and not period_end_time


def _open_shift_has_explicit_window(open_shift) -> bool:
    return bool(
        open_shift
        and (
            open_shift["shift_template_id"]
            or open_shift["period_start_time"]
            or open_shift["period_end_time"]
        )
    )


def _period_window_overlap_sql(timezone_param: str) -> str:
    return f"""
    AND NOT (
        COALESCE(
            period_end_time,
            (period_end::timestamp + INTERVAL '23:59:59') AT TIME ZONE {timezone_param}
        ) <= $2
        OR
        COALESCE(
            period_start_time,
            period_start::timestamp AT TIME ZONE {timezone_param}
        ) >= $3
    )
"""


async def _find_overlapping_period_id(
    conn,
    tenant_id: UUID,
    table: str,
    eff_start: datetime,
    eff_end: datetime,
    *,
    timezone_name: str = DEFAULT_TENANT_TIMEZONE,
    open_only: bool = False,
) -> Optional[UUID]:
    if table == "accounting_period":
        extra = "AND deleted_at IS NULL"
    elif table == "cash_shift_openings":
        extra = "AND status = 'open'" if open_only else ""
    else:
        raise ValueError(f"Unsupported overlap table: {table}")

    row = await conn.fetchrow(
        f"""
        SELECT id FROM {table}
        WHERE tenant_id = $1
          {extra}
          {_period_window_overlap_sql("$4")}
        LIMIT 1
        """,
        tenant_id, eff_start, eff_end, timezone_name,
    )
    return row["id"] if row else None


async def _fetch_open_shift_for_window(
    conn,
    tenant_id: UUID,
    eff_start: datetime,
    eff_end: datetime,
    timezone_name: str = DEFAULT_TENANT_TIMEZONE,
):
    return await conn.fetchrow(
        f"""
        SELECT
            id, opening_cash, opening_breakdown, opened_at, opened_by_user_id,
            shift_template_id, period_start, period_end,
            period_start_time, period_end_time
        FROM cash_shift_openings
        WHERE tenant_id = $1
          AND status = 'open'
          {_period_window_overlap_sql("$4")}
        ORDER BY opened_at DESC
        LIMIT 1
        """,
        tenant_id, eff_start, eff_end, timezone_name,
    )


def _open_shift_row_to_dict(row) -> dict:
    breakdown = row["opening_breakdown"]
    if isinstance(breakdown, str):
        breakdown = json.loads(breakdown)
    return {
        "id":                   str(row["id"]),
        "status":               "open",
        "openingCash":          float(row["opening_cash"]),
        "openingBreakdown":     breakdown,
        "periodStart":          row["period_start"].isoformat(),
        "periodEnd":            row["period_end"].isoformat(),
        "periodStartTime":      row["period_start_time"].isoformat() if row["period_start_time"] else None,
        "periodEndTime":        row["period_end_time"].isoformat() if row["period_end_time"] else None,
        "shiftTemplateId":      str(row["shift_template_id"]) if row["shift_template_id"] else None,
        "openedAt":             row["opened_at"].isoformat(),
        "openedByUserId":       str(row["opened_by_user_id"]) if row["opened_by_user_id"] else None,
    }


def _open_shift_list_row_to_dict(row) -> dict:
    """List-item shape for open shifts — aligns with closed rows where possible."""
    base = _open_shift_row_to_dict(row)
    template_name = row.get("shift_template_name")
    base.update({
        "shiftTemplateName": template_name,
        "accountingPeriodId":   None,
        "tenantId":             None,
        "totalSales":           None,
        "itemsSold":            None,
        "totalTips":            None,
        "totalTipTax":          None,
        "cashTips":             None,
        "totalCharged":         None,
        "totalCash":            None,
        "totalCard":            None,
        "totalDigital":         None,
        "totalCredit":          None,
        "gastosEfectivo":       None,
        "cashExpected":         None,
        "cashCounted":          None,
        "cashDifference":       None,
        "cashLeftInDrawer":     None,
        "notes":                None,
        "closedAt":             None,
    })
    return base


async def _fetch_tenant_default_opening_cash(conn, tenant_id: UUID) -> float:
    row = await conn.fetchrow(
        "SELECT default_opening_cash FROM tenants WHERE id = $1",
        tenant_id,
    )
    if not row or row["default_opening_cash"] is None:
        return 0.0
    return float(row["default_opening_cash"])


async def _resolve_suggested_opening_cash(
    conn,
    tenant_id: UUID,
    shift_template_id: Optional[UUID] = None,
) -> float:
    """Last declared leave-in-drawer for template, else tenant default (#922)."""
    row = await conn.fetchrow(
        """
        SELECT cs.cash_left_in_drawer
        FROM closing_summary cs
        JOIN accounting_period ap ON ap.id = cs.accounting_period_id
        WHERE cs.tenant_id = $1
          AND ap.deleted_at IS NULL
          AND cs.cash_left_in_drawer IS NOT NULL
          AND ($2::uuid IS NULL OR ap.shift_template_id = $2)
        ORDER BY ap.closed_at DESC
        LIMIT 1
        """,
        tenant_id,
        shift_template_id,
    )
    if row and row["cash_left_in_drawer"] is not None:
        return float(row["cash_left_in_drawer"])
    return await _fetch_tenant_default_opening_cash(conn, tenant_id)


def _compute_cash_expected(
    opening_cash: float,
    total_cash: float,
    gastos_efectivo: float,
    cash_purchases: float = 0.0,
) -> float:
    """Expected drawer cash: opening float + settled cash − cash outflows.

    ``total_cash`` comes from ``method_totals`` and already includes tip
    settlement assigned to cash, so tips must not be added a second time.
    """
    return opening_cash + total_cash - gastos_efectivo - cash_purchases


def _sum_advance_bucket(bucket: Dict[str, float]) -> float:
    return sum(float(total or 0.0) for total in bucket.values())


def _advance_audit_totals(advance_totals: Dict[str, Dict[str, float]]) -> Dict[str, float]:
    """Audit values used by cierre to explain table-session advance reconciliation."""
    return {
        "tableAdvanceCollections": _sum_advance_bucket(advance_totals.get("collections", {})),
        "tableAdvanceApplications": _sum_advance_bucket(advance_totals.get("applications", {})),
        "tableAdvanceCover": float(advance_totals.get("cover", {}).get("total", 0.0)),
    }


async def _compute_method_outflow_rows(
    conn,
    tenant_id: UUID,
    period_start: date,
    period_end: date,
    period_start_time: Optional[datetime] = None,
    period_end_time: Optional[datetime] = None,
    timezone_name: str = DEFAULT_TENANT_TIMEZONE,
) -> List[Dict[str, Any]]:
    expense_filter, expense_params = _build_expense_filter(
        period_start, period_end, period_start_time, period_end_time, table_alias="e"
    )
    purchase_filter, purchase_params = _build_purchase_payment_filter(
        period_start, period_end, period_start_time, period_end_time, timezone_name, table_alias="tp"
    )

    expense_rows = await conn.fetch(
        f"""
        SELECT
            COALESCE(pmg.slug, e.payment_method) AS group_slug,
            COALESCE(pm.name, e.payment_method) AS method_name,
            COALESCE(SUM(e.amount), 0) AS total
        FROM tenant_expenses e
        LEFT JOIN payment_methods pm ON pm.id = e.payment_method_id
        LEFT JOIN payment_method_groups pmg ON pmg.id = pm.group_id
        WHERE e.tenant_id = $1
          AND COALESCE(pmg.slug, e.payment_method) IS NOT NULL
          {expense_filter}
        GROUP BY COALESCE(pmg.slug, e.payment_method), COALESCE(pm.name, e.payment_method)
        """,
        tenant_id, *expense_params,
    )

    purchase_rows = await conn.fetch(
        f"""
        SELECT
            COALESCE(pmg.slug, tp.payment_method) AS group_slug,
            COALESCE(pm.name, tp.payment_method) AS method_name,
            COALESCE(SUM(tp.payment_amount), 0) AS total
        FROM tenant_purchases tp
        LEFT JOIN payment_methods pm ON pm.id = tp.payment_method_id
        LEFT JOIN payment_method_groups pmg ON pmg.id = pm.group_id
        WHERE tp.tenant_id = $1
          AND tp.status = 'paid'
          AND COALESCE(tp.payment_amount, 0) > 0
          AND COALESCE(pmg.slug, tp.payment_method) IS NOT NULL
          {purchase_filter}
        GROUP BY COALESCE(pmg.slug, tp.payment_method), COALESCE(pm.name, tp.payment_method)
        """,
        tenant_id, *purchase_params,
    )

    aggregated: Dict[tuple, Dict[str, Any]] = {}
    for row in expense_rows:
        key = (row["group_slug"], row["method_name"])
        aggregated.setdefault(key, {
            "group_slug": row["group_slug"],
            "method_name": row["method_name"],
            "expense_outflows_amount": 0.0,
            "purchase_outflows_amount": 0.0,
        })
        aggregated[key]["expense_outflows_amount"] += float(row["total"] or 0)

    for row in purchase_rows:
        key = (row["group_slug"], row["method_name"])
        aggregated.setdefault(key, {
            "group_slug": row["group_slug"],
            "method_name": row["method_name"],
            "expense_outflows_amount": 0.0,
            "purchase_outflows_amount": 0.0,
        })
        aggregated[key]["purchase_outflows_amount"] += float(row["total"] or 0)

    return [
        row for row in aggregated.values()
        if row["expense_outflows_amount"] or row["purchase_outflows_amount"]
    ]


def _merge_breakdown_with_outflows(
    inflow_rows: List[Dict[str, Any]],
    outflow_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    aggregated: Dict[tuple, Dict[str, Any]] = {}

    for row in inflow_rows:
        key = (row["group_slug"], row["method_name"])
        aggregated[key] = {
            "group_slug": row["group_slug"],
            "method_name": row["method_name"],
            "total": float(row.get("total") or 0),
            "gross_inflows_amount": float(row.get("gross_inflows_amount", row.get("total", 0)) or 0),
            "expense_outflows_amount": float(row.get("expense_outflows_amount") or 0),
            "purchase_outflows_amount": float(row.get("purchase_outflows_amount") or 0),
        }

    for row in outflow_rows:
        key = (row["group_slug"], row["method_name"])
        if key not in aggregated:
            aggregated[key] = {
                "group_slug": row["group_slug"],
                "method_name": row["method_name"],
                "total": 0.0,
                "gross_inflows_amount": 0.0,
                "expense_outflows_amount": 0.0,
                "purchase_outflows_amount": 0.0,
            }
        aggregated[key]["expense_outflows_amount"] += float(row.get("expense_outflows_amount") or 0)
        aggregated[key]["purchase_outflows_amount"] += float(row.get("purchase_outflows_amount") or 0)

    for row in aggregated.values():
        row["expected_amount"] = (
            float(row["gross_inflows_amount"])
            - float(row["expense_outflows_amount"])
            - float(row["purchase_outflows_amount"])
        )

    return [
        row for row in aggregated.values()
        if (
            row["gross_inflows_amount"]
            or row["expense_outflows_amount"]
            or row["purchase_outflows_amount"]
        )
    ]


def _apply_table_session_advances_to_methods(
    method_totals: Dict[str, float],
    advance_totals: Dict[str, Dict[str, float]],
) -> Dict[str, float]:
    """Move table advances from order settlement methods to their tender methods.

    Example: with a 60k digital advance and a 100k cash close, the order can
    carry 100k cash settlement. Cierre subtracts the 60k applied advance from
    cash and then adds the 60k digital collection, leaving cash=40k,
    digital=60k. Exact/full-advance closes settle against table_session_advance
    and are reduced to zero before adding the original tender collection.
    """
    adjusted = dict(method_totals)
    for method, total in advance_totals.get("applications", {}).items():
        adjusted[method] = max(adjusted.get(method, 0.0) - float(total or 0.0), 0.0)
    for method, total in advance_totals.get("collections", {}).items():
        adjusted[method] = adjusted.get(method, 0.0) + float(total or 0.0)
    return adjusted


async def _compute_preview(
    conn,
    tenant_id: UUID,
    period_start: date,
    period_end: date,
    completed_only: bool = False,
    period_start_time: Optional[datetime] = None,
    period_end_time: Optional[datetime] = None,
    opening_cash: float = 0.0,
    timezone_name: str = DEFAULT_TENANT_TIMEZONE,
) -> dict:
    """
    Runs the three aggregation queries (sales, gastos, open tables) and returns
    a plain dict. Used by both get_cierre_preview and create_cierre.

    completed_only=True  → only 'completed' orders (used for the actual Cierre Z)
    completed_only=False → 'completed' + 'pending' (used for Cierre X preview so
                           open-table orders are visible)

    When period_start_time / period_end_time are supplied the order, expense,
    and open-table filters use exact TIMESTAMPTZ comparison (shift windows).
    """
    status_filter = "AND status = 'completed'" if completed_only else "AND status IN ('completed', 'pending')"
    date_filter, date_params = _build_order_date_filter(
        period_start, period_end, period_start_time, period_end_time, timezone_name
    )
    expense_filter, expense_params = _build_expense_filter(
        period_start, period_end, period_start_time, period_end_time
    )
    open_tables_filter, open_tables_params = _build_open_tables_filter(
        period_start, period_end, period_start_time, period_end_time, timezone_name
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

    tips_row = await conn.fetchrow(
        f"""
        SELECT
            COALESCE(SUM(tip_amount), 0)      AS total_tips,
            COALESCE(SUM(tip_tax_amount), 0)  AS total_tip_tax
        FROM orders
        WHERE tenant_id = $1
          {status_filter}
          {date_filter}
        """,
        tenant_id, *date_params,
    )

    cash_tips_row = await conn.fetchrow(
        f"""
        SELECT COALESCE(SUM(o.tip_amount + o.tip_tax_amount), 0) AS cash_tips
        FROM orders o
        LEFT JOIN payment_methods pm ON pm.id = o.payment_method_id
        LEFT JOIN payment_method_groups pmg ON pmg.id = pm.group_id
        WHERE o.tenant_id = $1
          {status_filter.replace('status', 'o.status')}
          {date_filter.replace('order_date', 'o.order_date')}
          AND (o.tip_amount > 0 OR o.tip_tax_amount > 0)
          AND COALESCE(pmg.slug, o.payment_method) = 'cash'
        """,
        tenant_id, *date_params,
    )

    # Payment method totals — COALESCE split vs legacy:
    # Split orders: sum from active order_payments rows
    # Legacy orders (no active order_payments rows): use orders.total_amount + stored method
    method_rows = await conn.fetch(
        f"""
        SELECT
            COALESCE(pmg.slug, op.payment_method) AS method,
            COALESCE(SUM(op.amount), 0) AS total
        FROM order_payments op
        JOIN orders o ON o.id = op.order_id
        LEFT JOIN payment_methods pm ON pm.id = op.payment_method_id
        LEFT JOIN payment_method_groups pmg ON pmg.id = pm.group_id
        WHERE o.tenant_id = $1
          {status_filter.replace('status', 'o.status')}
          {date_filter.replace('order_date', 'o.order_date')}
          AND op.voided_at IS NULL
        GROUP BY COALESCE(pmg.slug, op.payment_method)

        UNION ALL

        SELECT
            pmg.slug AS method,
            COALESCE(SUM(total_amount), 0) AS total
        FROM orders o
        JOIN payment_methods pm ON pm.id = o.payment_method_id
        JOIN payment_method_groups pmg ON pmg.id = pm.group_id
        WHERE o.tenant_id = $1
          {status_filter}
          {date_filter}
          AND o.payment_method_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM order_payments op
              WHERE op.order_id = o.id
                AND op.voided_at IS NULL
          )
        GROUP BY pmg.slug

        UNION ALL

        SELECT
            o.payment_method AS method,
            COALESCE(SUM(o.total_amount), 0) AS total
        FROM orders o
        WHERE o.tenant_id = $1
          {status_filter}
          {date_filter}
          AND o.payment_method_id IS NULL
          AND o.payment_method IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM order_payments op
              WHERE op.order_id = o.id
                AND op.voided_at IS NULL
          )
        GROUP BY o.payment_method

        UNION ALL

        -- Tip settlement on order header (single-pay and split completion)
        SELECT
            pmg.slug AS method,
            COALESCE(SUM(o.tip_amount + o.tip_tax_amount), 0) AS total
        FROM orders o
        JOIN payment_methods pm ON pm.id = o.payment_method_id
        JOIN payment_method_groups pmg ON pmg.id = pm.group_id
        WHERE o.tenant_id = $1
          {status_filter}
          {date_filter}
          AND o.payment_method_id IS NOT NULL
          AND (o.tip_amount > 0 OR o.tip_tax_amount > 0)
        GROUP BY pmg.slug

        UNION ALL

        SELECT
            o.payment_method AS method,
            COALESCE(SUM(o.tip_amount + o.tip_tax_amount), 0) AS total
        FROM orders o
        WHERE o.tenant_id = $1
          {status_filter}
          {date_filter}
          AND o.payment_method_id IS NULL
          AND o.payment_method IS NOT NULL
          AND (o.tip_amount > 0 OR o.tip_tax_amount > 0)
        GROUP BY o.payment_method
        """,
        tenant_id, *date_params,
    )

    # Aggregate method totals in Python to handle UNION ALL correctly
    method_totals: Dict[str, float] = {}
    for row in method_rows:
        m = row["method"]
        if m:
            method_totals[m] = method_totals.get(m, 0.0) + float(row["total"])

    from app.services.customer_wallet_service import fetch_wallet_recharge_totals_for_cierre
    from app.services.table_session_advances_service import fetch_table_session_advance_totals_for_cierre

    recharge_totals = await fetch_wallet_recharge_totals_for_cierre(
        conn,
        tenant_id,
        period_start,
        period_end,
        period_start_time,
        period_end_time,
    )
    for method, total in recharge_totals.items():
        method_totals[method] = method_totals.get(method, 0.0) + total

    advance_totals = await fetch_table_session_advance_totals_for_cierre(
        conn,
        tenant_id,
        period_start,
        period_end,
        period_start_time,
        period_end_time,
    )
    method_totals = _apply_table_session_advances_to_methods(method_totals, advance_totals)
    advance_audit = _advance_audit_totals(advance_totals)
    minimum_cover_income = float(advance_totals.get("cover", {}).get("total", 0.0))

    gastos_row = await conn.fetchrow(
        f"""
        SELECT COALESCE(SUM(amount), 0) AS gastos_efectivo
        FROM tenant_expenses
        WHERE tenant_id = $1
          AND payment_method = 'cash'
          {expense_filter}
        """,
        tenant_id, *expense_params,
    )

    purchase_filter, purchase_params = _build_purchase_payment_filter(
        period_start, period_end, period_start_time, period_end_time, timezone_name, table_alias="tp"
    )
    cash_purchases_row = await conn.fetchrow(
        f"""
        SELECT COALESCE(SUM(tp.payment_amount), 0) AS cash_purchases
        FROM tenant_purchases tp
        LEFT JOIN payment_methods pm ON pm.id = tp.payment_method_id
        LEFT JOIN payment_method_groups pmg ON pmg.id = pm.group_id
        WHERE tp.tenant_id = $1
          AND tp.status = 'paid'
          AND COALESCE(tp.payment_amount, 0) > 0
          AND COALESCE(pmg.slug, tp.payment_method) = 'cash'
          {purchase_filter}
        """,
        tenant_id, *purchase_params,
    )

    open_tables_row = await conn.fetchrow(
        f"""
        SELECT COUNT(*) AS open_tables_count
        FROM table_sessions ts
        JOIN tables t ON t.id = ts.table_id
        WHERE ts.tenant_id = $1
          AND ts.closed_at IS NULL
          AND ts.is_discarded = FALSE
          {open_tables_filter}
          AND t.is_bar IS FALSE
        """,
        tenant_id, *open_tables_params,
    )

    total_cash = method_totals.get("cash", 0.0)
    gastos_efectivo = float(gastos_row["gastos_efectivo"])
    cash_purchases = float(cash_purchases_row["cash_purchases"])
    total_tips = float(tips_row["total_tips"])
    total_tip_tax = float(tips_row["total_tip_tax"])
    cash_tips = float(cash_tips_row["cash_tips"])
    total_charged = float(sales_row["total_sales"]) + minimum_cover_income + tip_settlement_total(
        total_tips, total_tip_tax,
    )
    cash_expected = _compute_cash_expected(
        float(opening_cash),
        total_cash,
        gastos_efectivo,
        cash_purchases,
    )

    return {
        "totalSales":       float(sales_row["total_sales"]),
        "minimumCoverIncome": minimum_cover_income,
        "itemsSold":        int(sales_row["items_sold"]),
        "totalTips":        total_tips,
        "totalTipTax":      total_tip_tax,
        "totalCharged":     total_charged,
        **advance_audit,
        "cashTips":         cash_tips,
        "openingCash":      float(opening_cash),
        "totalCash":        total_cash,
        "totalCard":        method_totals.get("card", 0.0),
        "totalDigital":     method_totals.get("digital", 0.0),
        "totalCredit":      method_totals.get("credit", 0.0),
        "gastosEfectivo":   gastos_efectivo,
        "cashPurchases":    cash_purchases,
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
    timezone_name: str = DEFAULT_TENANT_TIMEZONE,
) -> List[Dict[str, Any]]:
    """
    Compute per-method payment totals for the period via UNION ALL:
      - Modern orders (payment_method_id IS NOT NULL): join payment_methods + payment_method_groups
      - Legacy orders (payment_method_id IS NULL): group by payment_method VARCHAR slug

    Returns list of {group_slug, method_name, total}, excluding zero-total rows.
    Product amounts come from order_payments or orders.total_amount; tips are
    attributed to each order's closing payment method (order header, not split
    payment rows). Orders without an active payment method stay visible as
    "Sin método registrado" so the breakdown can reconcile with totalCharged.
    When period_start_time / period_end_time are supplied, uses exact TIMESTAMPTZ comparison.
    """
    status_filter = "AND status = 'completed'" if completed_only else "AND status IN ('completed', 'pending')"
    date_filter, date_params = _build_order_date_filter(
        period_start, period_end, period_start_time, period_end_time, timezone_name
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
          AND op.voided_at IS NULL
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
          AND NOT EXISTS (
              SELECT 1
              FROM order_payments op
              WHERE op.order_id = o.id
                AND op.voided_at IS NULL
          )
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
          AND NOT EXISTS (
              SELECT 1
              FROM order_payments op
              WHERE op.order_id = o.id
                AND op.voided_at IS NULL
          )
        GROUP BY o.payment_method

        UNION ALL

        -- Orders with no active payment tracking yet: keep them visible so the
        -- close preview explains why totalSales can exceed registered methods.
        SELECT
            'untracked'                AS group_slug,
            'Sin método registrado'    AS method_name,
            COALESCE(SUM(o.total_amount), 0) AS total
        FROM orders o
        WHERE o.tenant_id = $1
          {status_filter}
          {date_filter}
          AND o.payment_method_id IS NULL
          AND o.payment_method IS NULL
          AND NOT EXISTS (
              SELECT 1
              FROM order_payments op
              WHERE op.order_id = o.id
                AND op.voided_at IS NULL
          )

        UNION ALL

        -- Tip settlement on order closing method (FK; covers split completion)
        SELECT
            pmg.slug        AS group_slug,
            pm.name         AS method_name,
            COALESCE(SUM(o.tip_amount + o.tip_tax_amount), 0) AS total
        FROM orders o
        JOIN payment_methods pm ON pm.id = o.payment_method_id
        JOIN payment_method_groups pmg ON pmg.id = pm.group_id
        WHERE o.tenant_id = $1
          {status_filter}
          {date_filter}
          AND o.payment_method_id IS NOT NULL
          AND (o.tip_amount > 0 OR o.tip_tax_amount > 0)
        GROUP BY pmg.slug, pm.name

        UNION ALL

        -- Tip settlement for legacy VARCHAR method
        SELECT
            o.payment_method AS group_slug,
            o.payment_method AS method_name,
            COALESCE(SUM(o.tip_amount + o.tip_tax_amount), 0) AS total
        FROM orders o
        WHERE o.tenant_id = $1
          {status_filter}
          {date_filter}
          AND o.payment_method_id IS NULL
          AND o.payment_method IS NOT NULL
          AND (o.tip_amount > 0 OR o.tip_tax_amount > 0)
        GROUP BY o.payment_method

        UNION ALL

        -- Tip settlement with no payment method tracked
        SELECT
            'untracked'                AS group_slug,
            'Sin método registrado'    AS method_name,
            COALESCE(SUM(o.tip_amount + o.tip_tax_amount), 0) AS total
        FROM orders o
        WHERE o.tenant_id = $1
          {status_filter}
          {date_filter}
          AND o.payment_method_id IS NULL
          AND o.payment_method IS NULL
          AND (o.tip_amount > 0 OR o.tip_tax_amount > 0)
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
    from app.services.table_session_advances_service import fetch_table_session_advance_totals_for_cierre
    advance_totals = await fetch_table_session_advance_totals_for_cierre(
        conn,
        tenant_id,
        period_start,
        period_end,
        period_start_time,
        period_end_time,
    )
    for method, total in advance_totals.get("applications", {}).items():
        for key, row in aggregated.items():
            if key[0] == method:
                row["total"] = max(float(row["total"]) - float(total or 0.0), 0.0)
                break
    for method, total in advance_totals.get("collections", {}).items():
        if total == 0:
            continue
        key = (method, f"Anticipo mesa - {method}")
        if key not in aggregated:
            aggregated[key] = {
                "group_slug": method,
                "method_name": f"Anticipo mesa - {method}",
                "total": float(total),
            }
        else:
            aggregated[key]["total"] += float(total)
    return [r for r in aggregated.values() if r["total"] > 0]


# ---------------------------------------------------------------------------
# Shift template resolution (#686)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ResolvedCierrePeriod:
    period_start: date
    period_end: date
    period_start_time: Optional[datetime]
    period_end_time: Optional[datetime]
    shift_template_id: Optional[UUID]


@dataclass(frozen=True)
class DayWindowResolution:
    resolved: ResolvedCierrePeriod
    is_partial: bool


async def resolve_cierre_period_fields(
    conn,
    tenant_id: UUID,
    *,
    period_start: date,
    period_end: date,
    period_start_time: Optional[datetime],
    period_end_time: Optional[datetime],
    shift_template_id: Optional[UUID],
    timezone_name: str = DEFAULT_TENANT_TIMEZONE,
) -> ResolvedCierrePeriod:
    """Resolve template vs custom vs full-day period fields before preview/create."""
    if shift_template_id:
        if period_start_time or period_end_time:
            raise APIError(
                "No envíes horas manuales cuando usas una plantilla de turno.",
                status_code=422,
            )
        if period_start != period_end:
            raise APIError(
                "La plantilla de turno solo aplica a un solo día.",
                status_code=422,
            )

        row = await conn.fetchrow(
            """
            SELECT id, name, start_time, end_time, crosses_midnight
            FROM tenant_shift_templates
            WHERE id = $1 AND tenant_id = $2 AND is_active = true
            """,
            shift_template_id,
            tenant_id,
        )
        if not row:
            raise APIError("Plantilla de turno no encontrada o inactiva.", status_code=404)

        from app.services.shift_window_service import resolve_shift_template_window

        payload = resolve_shift_template_window(
            anchor_date=period_start,
            start_time=row["start_time"],
            end_time=row["end_time"],
            crosses_midnight=row["crosses_midnight"],
            template_id=row["id"],
            template_name=row["name"],
            timezone_name=timezone_name,
        )
        return ResolvedCierrePeriod(
            period_start=date.fromisoformat(payload["periodStart"]),
            period_end=date.fromisoformat(payload["periodEnd"]),
            period_start_time=datetime.fromisoformat(payload["periodStartTime"]),
            period_end_time=datetime.fromisoformat(payload["periodEndTime"]),
            shift_template_id=shift_template_id,
        )

    return ResolvedCierrePeriod(
        period_start=period_start,
        period_end=period_end,
        period_start_time=period_start_time,
        period_end_time=period_end_time,
        shift_template_id=None,
    )


def _resolve_remaining_day_window_from_rows(
    day: date,
    rows,
    *,
    timezone_name: str = DEFAULT_TENANT_TIMEZONE,
) -> DayWindowResolution:
    day_start, day_end = _effective_period_bounds(day, day, None, None, timezone_name)
    latest_covered_end = None
    for row in rows:
        row_start, row_end = _effective_period_bounds(
            row["period_start"],
            row["period_end"],
            row["period_start_time"],
            row["period_end_time"],
            timezone_name,
        )
        if row_end <= day_start or row_start >= day_end:
            continue
        if row_end > day_start and (latest_covered_end is None or row_end > latest_covered_end):
            latest_covered_end = row_end

    if latest_covered_end is None:
        return DayWindowResolution(
            resolved=ResolvedCierrePeriod(day, day, None, None, None),
            is_partial=False,
        )

    if latest_covered_end >= day_end:
        raise APIError(
            "Este día ya está completamente cubierto por cierres anteriores.",
            status_code=409,
        )

    period_start = latest_covered_end.astimezone(get_zoneinfo(timezone_name)).date()
    return DayWindowResolution(
        resolved=ResolvedCierrePeriod(
            period_start=period_start,
            period_end=day,
            period_start_time=latest_covered_end,
            period_end_time=day_end,
            shift_template_id=None,
        ),
        is_partial=True,
    )


async def _fetch_day_overlapping_periods(
    conn,
    tenant_id: UUID,
    day: date,
    *,
    timezone_name: str = DEFAULT_TENANT_TIMEZONE,
):
    day_start, day_end = _effective_period_bounds(day, day, None, None, timezone_name)
    return await conn.fetch(
        f"""
        SELECT id, period_start, period_end, period_start_time, period_end_time
        FROM accounting_period
        WHERE tenant_id = $1
          AND deleted_at IS NULL
          {_period_window_overlap_sql("$4")}
        ORDER BY COALESCE(
            period_end_time,
            (period_end::timestamp + INTERVAL '23:59:59') AT TIME ZONE $4
        ) DESC
        """,
        tenant_id,
        day_start,
        day_end,
        timezone_name,
    )


async def _resolve_day_only_period(
    conn,
    tenant_id: UUID,
    resolved: ResolvedCierrePeriod,
    *,
    timezone_name: str = DEFAULT_TENANT_TIMEZONE,
) -> DayWindowResolution:
    if not _is_day_only_cierre_request(
        resolved.shift_template_id,
        resolved.period_start_time,
        resolved.period_end_time,
    ):
        return DayWindowResolution(resolved=resolved, is_partial=False)
    if resolved.period_start != resolved.period_end:
        return DayWindowResolution(resolved=resolved, is_partial=False)

    rows = await _fetch_day_overlapping_periods(
        conn,
        tenant_id,
        resolved.period_start,
        timezone_name=timezone_name,
    )
    return _resolve_remaining_day_window_from_rows(
        resolved.period_start,
        rows,
        timezone_name=timezone_name,
    )


def _day_window_to_dict(window: DayWindowResolution) -> dict:
    resolved = window.resolved
    return {
        "periodStart": resolved.period_start.isoformat(),
        "periodEnd": resolved.period_end.isoformat(),
        "periodStartTime": (
            resolved.period_start_time.isoformat() if resolved.period_start_time else None
        ),
        "periodEndTime": (
            resolved.period_end_time.isoformat() if resolved.period_end_time else None
        ),
        "isPartial": window.is_partial,
        "windowLabel": "Día restante" if window.is_partial else "Día completo",
    }


async def get_day_window(request: Request, anchor_date: date) -> dict:
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection(use_transaction=False) as conn:
            timezone_name = await resolve_tenant_timezone(conn, tenant_id)
            base = ResolvedCierrePeriod(anchor_date, anchor_date, None, None, None)
            window = await _resolve_day_only_period(
                conn,
                tenant_id,
                base,
                timezone_name=timezone_name,
            )
            return {"success": True, "data": _day_window_to_dict(window)}

    except (AuthenticationError, APIError):
        raise
    except Exception as exc:
        logger.error(f"Error in get_day_window: {exc}")
        raise APIError(f"Error in get_day_window: {exc}", status_code=500)


async def list_active_shift_templates(request: Request) -> dict:
    """Active shift templates for Finanzas arqueo UI (warocol.com#686)."""
    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    async with get_db_connection(use_transaction=False) as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, start_time, end_time, crosses_midnight, sort_order
            FROM tenant_shift_templates
            WHERE tenant_id = $1 AND is_active = true
            ORDER BY sort_order, name
            """,
            tenant_id,
        )

    data = []
    for row in rows:
        data.append({
            "id": str(row["id"]),
            "name": row["name"],
            "startTime": row["start_time"].strftime("%H:%M"),
            "endTime": row["end_time"].strftime("%H:%M"),
            "crossesMidnight": row["crosses_midnight"],
        })
    return {"success": True, "data": data}


# ---------------------------------------------------------------------------
# POST /cierre/open-shift + GET /cierre/shift-status (#920)
# ---------------------------------------------------------------------------

async def open_shift(request: Request, body: OpenShiftCreate) -> dict:
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection(use_transaction=True) as conn:
            timezone_name = await resolve_tenant_timezone(conn, tenant_id)
            resolved = await resolve_cierre_period_fields(
                conn,
                tenant_id,
                period_start=body.period_start,
                period_end=body.period_end,
                period_start_time=body.period_start_time,
                period_end_time=body.period_end_time,
                shift_template_id=body.shift_template_id,
                timezone_name=timezone_name,
            )
            resolved = (
                await _resolve_day_only_period(
                    conn,
                    tenant_id,
                    resolved,
                    timezone_name=timezone_name,
                )
            ).resolved
            eff_start, eff_end = _effective_period_bounds(
                resolved.period_start,
                resolved.period_end,
                resolved.period_start_time,
                resolved.period_end_time,
                timezone_name,
            )

            if await _find_overlapping_period_id(
                conn, tenant_id, "accounting_period", eff_start, eff_end,
                timezone_name=timezone_name,
            ):
                raise APIError(
                    "Ya existe un cierre cerrado para este período o uno que se superpone.",
                    status_code=409,
                )

            if await _find_overlapping_period_id(
                conn, tenant_id, "cash_shift_openings", eff_start, eff_end,
                timezone_name=timezone_name, open_only=True,
            ):
                raise APIError(
                    "Ya hay un turno abierto para este período o uno que se superpone.",
                    status_code=409,
                )

            await check_plan_quota_growth(conn, tenant_id, "active_open_cash_shifts")

            breakdown_json = (
                json.dumps(body.opening_breakdown)
                if body.opening_breakdown is not None
                else None
            )
            row = await conn.fetchrow(
                """
                INSERT INTO cash_shift_openings (
                    tenant_id, shift_template_id,
                    period_start, period_end, period_start_time, period_end_time,
                    opening_cash, opening_breakdown, opened_by_user_id
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9)
                RETURNING
                    id, opening_cash, opening_breakdown, opened_at, opened_by_user_id,
                    shift_template_id, period_start, period_end,
                    period_start_time, period_end_time
                """,
                tenant_id,
                resolved.shift_template_id,
                resolved.period_start,
                resolved.period_end,
                resolved.period_start_time,
                resolved.period_end_time,
                body.opening_cash,
                breakdown_json,
                session_context.user_id,
            )

        return {"success": True, "data": _open_shift_row_to_dict(row)}

    except (AuthenticationError, APIError):
        raise
    except Exception as exc:
        logger.error(f"Error in open_shift: {exc}")
        raise APIError(f"Error in open_shift: {exc}", status_code=500)


async def get_shift_status(
    request: Request,
    period_start: date,
    period_end: date,
    period_start_time: Optional[datetime] = None,
    period_end_time: Optional[datetime] = None,
    shift_template_id: Optional[UUID] = None,
) -> dict:
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection(use_transaction=False) as conn:
            timezone_name = await resolve_tenant_timezone(conn, tenant_id)
            resolved = await resolve_cierre_period_fields(
                conn,
                tenant_id,
                period_start=period_start,
                period_end=period_end,
                period_start_time=period_start_time,
                period_end_time=period_end_time,
                shift_template_id=shift_template_id,
                timezone_name=timezone_name,
            )
            window = await _resolve_day_only_period(
                conn,
                tenant_id,
                resolved,
                timezone_name=timezone_name,
            )
            resolved = window.resolved
            eff_start, eff_end = _effective_period_bounds(
                resolved.period_start,
                resolved.period_end,
                resolved.period_start_time,
                resolved.period_end_time,
                timezone_name,
            )
            row = await _fetch_open_shift_for_window(
                conn, tenant_id, eff_start, eff_end, timezone_name
            )

            if not row:
                suggested = await _resolve_suggested_opening_cash(
                    conn, tenant_id, resolved.shift_template_id,
                )
                return {
                    "success": True,
                    "data": {
                        "status": "none",
                        "suggestedOpeningCash": suggested,
                        **_day_window_to_dict(window),
                    },
                }

            data = _open_shift_row_to_dict(row)
            data.update(_day_window_to_dict(window))
            return {"success": True, "data": data}

    except (AuthenticationError, APIError):
        raise
    except Exception as exc:
        logger.error(f"Error in get_shift_status: {exc}")
        raise APIError(f"Error in get_shift_status: {exc}", status_code=500)


async def get_cash_settings(request: Request) -> dict:
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection(use_transaction=False) as conn:
            default_cash = await _fetch_tenant_default_opening_cash(conn, tenant_id)

        return {"success": True, "data": {"defaultOpeningCash": default_cash}}

    except (AuthenticationError, APIError):
        raise
    except Exception as exc:
        logger.error(f"Error in get_cash_settings: {exc}")
        raise APIError(f"Error in get_cash_settings: {exc}", status_code=500)


async def update_cash_settings(request: Request, body: CierreCashSettingsUpdate) -> dict:
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection(use_transaction=True) as conn:
            await conn.execute(
                """
                UPDATE tenants
                SET default_opening_cash = $2
                WHERE id = $1
                """,
                tenant_id,
                body.default_opening_cash,
            )
            default_cash = await _fetch_tenant_default_opening_cash(conn, tenant_id)

        return {"success": True, "data": {"defaultOpeningCash": default_cash}}

    except (AuthenticationError, APIError):
        raise
    except Exception as exc:
        logger.error(f"Error in update_cash_settings: {exc}")
        raise APIError(f"Error in update_cash_settings: {exc}", status_code=500)


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
    shift_template_id: Optional[UUID] = None,
) -> dict:
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection(use_transaction=False) as conn:
            timezone_name = await resolve_tenant_timezone(conn, tenant_id)
            resolved = await resolve_cierre_period_fields(
                conn,
                tenant_id,
                period_start=period_start,
                period_end=period_end,
                period_start_time=period_start_time,
                period_end_time=period_end_time,
                shift_template_id=shift_template_id,
                timezone_name=timezone_name,
            )
            resolved = (
                await _resolve_day_only_period(
                    conn,
                    tenant_id,
                    resolved,
                    timezone_name=timezone_name,
                )
            ).resolved
            eff_start, eff_end = _effective_period_bounds(
                resolved.period_start,
                resolved.period_end,
                resolved.period_start_time,
                resolved.period_end_time,
                timezone_name,
            )
            open_shift = await _fetch_open_shift_for_window(
                conn, tenant_id, eff_start, eff_end, timezone_name,
            )
            opening_cash = float(open_shift["opening_cash"]) if open_shift else 0.0
            preview = await _compute_preview(
                conn, tenant_id,
                resolved.period_start, resolved.period_end,
                completed_only=completed_only,
                period_start_time=resolved.period_start_time,
                period_end_time=resolved.period_end_time,
                opening_cash=opening_cash,
                timezone_name=timezone_name,
            )
            breakdown = await _compute_breakdown_rows(
                conn, tenant_id,
                resolved.period_start, resolved.period_end,
                completed_only=completed_only,
                period_start_time=resolved.period_start_time,
                period_end_time=resolved.period_end_time,
                timezone_name=timezone_name,
            )
            outflows = await _compute_method_outflow_rows(
                conn, tenant_id,
                resolved.period_start, resolved.period_end,
                period_start_time=resolved.period_start_time,
                period_end_time=resolved.period_end_time,
                timezone_name=timezone_name,
            )
            preview["breakdown"] = _merge_breakdown_with_outflows(breakdown, outflows)

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
            timezone_name = await resolve_tenant_timezone(conn, tenant_id)
            resolved = await resolve_cierre_period_fields(
                conn,
                tenant_id,
                period_start=body.period_start,
                period_end=body.period_end,
                period_start_time=body.period_start_time,
                period_end_time=body.period_end_time,
                shift_template_id=body.shift_template_id,
                timezone_name=timezone_name,
            )
            resolved = (
                await _resolve_day_only_period(
                    conn,
                    tenant_id,
                    resolved,
                    timezone_name=timezone_name,
                )
            ).resolved
            period_start = resolved.period_start
            period_end = resolved.period_end
            period_start_time = resolved.period_start_time
            period_end_time = resolved.period_end_time
            shift_template_id = resolved.shift_template_id

            # 0. Validation: multi-day periods require exact timestamps
            if period_start != period_end and not (period_start_time and period_end_time):
                raise APIError(
                    "Para períodos de varios días debes especificar hora de inicio y fin exactas.",
                    status_code=422,
                )

            eff_start, eff_end = _effective_period_bounds(
                period_start, period_end, period_start_time, period_end_time, timezone_name,
            )

            # 1. Unified overlap check using effective time windows.
            overlap = await _find_overlapping_period_id(
                conn, tenant_id, "accounting_period", eff_start, eff_end,
                timezone_name=timezone_name,
            )
            if overlap:
                raise APIError(
                    "Ya existe un cierre para este período o uno que se superpone.",
                    status_code=409,
                )

            await check_plan_quota_period(conn, tenant_id, "cash_closes_per_period")

            open_shift = await _fetch_open_shift_for_window(
                conn, tenant_id, eff_start, eff_end, timezone_name,
            )
            if _open_shift_has_explicit_window(open_shift) and _is_day_only_cierre_request(
                shift_template_id, period_start_time, period_end_time,
            ):
                raise APIError(
                    "Hay un turno de caja abierto para esta fecha. "
                    "Cierra usando el turno seleccionado o envía la ventana exacta del turno.",
                    status_code=422,
                )
            if _requires_open_shift(shift_template_id, period_start_time, period_end_time):
                if not open_shift:
                    raise APIError(
                        "Debes abrir el turno con el fondo de caja antes de registrar el cierre.",
                        status_code=422,
                    )
            opening_cash = float(open_shift["opening_cash"]) if open_shift else 0.0

            # 2. Preview aggregation (completed only — cash already received)
            preview = await _compute_preview(
                conn, tenant_id, period_start, period_end,
                completed_only=True,
                period_start_time=period_start_time,
                period_end_time=period_end_time,
                opening_cash=opening_cash,
                timezone_name=timezone_name,
            )

            # 3. Open tables check — skip for past periods (mesas actuales no pertenecen al período)
            # Use tenant-local date so the check is correct even when the server runs in UTC.
            is_past_period = period_end < tenant_today(timezone_name, datetime.now())
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
                    (tenant_id, period_start, period_end, period_start_time, period_end_time, shift_template_id)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id, closed_at
                """,
                tenant_id, period_start, period_end,
                period_start_time, period_end_time, shift_template_id,
            )
            period_id = period_row["id"]
            closed_at = period_row["closed_at"]

            # 5. INSERT closing_summary
            cash_difference = body.cash_counted - preview["cashExpected"]
            cash_left = (
                body.cash_left_in_drawer
                if body.cash_left_in_drawer is not None
                else body.cash_counted
            )
            summary_row = await conn.fetchrow(
                """
                INSERT INTO closing_summary (
                    accounting_period_id, tenant_id,
                    total_sales, items_sold,
                    total_tips, total_tip_tax, cash_tips,
                    total_cash, total_card, total_digital, total_credit,
                    gastos_efectivo, cash_purchases, opening_cash, cash_expected, cash_counted, cash_difference,
                    cash_left_in_drawer, notes
                ) VALUES (
                    $1, $2,
                    $3, $4,
                    $5, $6, $7,
                    $8, $9, $10, $11,
                    $12, $13, $14, $15, $16, $17,
                    $18, $19
                )
                RETURNING id, created_at
                """,
                period_id, tenant_id,
                preview["totalSales"], preview["itemsSold"],
                preview["totalTips"], preview["totalTipTax"], preview["cashTips"],
                preview["totalCash"], preview["totalCard"],
                preview["totalDigital"], preview["totalCredit"],
                preview["gastosEfectivo"], preview["cashPurchases"],
                opening_cash, preview["cashExpected"],
                body.cash_counted, cash_difference,
                cash_left,
                body.notes,
            )

            if open_shift:
                await conn.execute(
                    """
                    UPDATE cash_shift_openings
                    SET status = 'closed',
                        accounting_period_id = $2,
                        closed_at = NOW()
                    WHERE id = $1 AND tenant_id = $3 AND status = 'open'
                    """,
                    open_shift["id"], period_id, tenant_id,
                )

            # 6. Compute and persist payment breakdown
            breakdown_rows = await _compute_breakdown_rows(
                conn, tenant_id, period_start, period_end,
                completed_only=True,
                period_start_time=period_start_time,
                period_end_time=period_end_time,
                timezone_name=timezone_name,
            )
            outflow_rows = await _compute_method_outflow_rows(
                conn, tenant_id, period_start, period_end,
                period_start_time=period_start_time,
                period_end_time=period_end_time,
                timezone_name=timezone_name,
            )
            breakdown_rows = _merge_breakdown_with_outflows(breakdown_rows, outflow_rows)
            if breakdown_rows:
                reported_map = {}
                for item in body.payment_breakdown_reported or []:
                    group_slug = item.group_slug
                    method_name = item.method_name
                    if group_slug and method_name:
                        reported_map[(group_slug, method_name)] = item.reported_amount
                reported_amounts = []
                differences = []
                statuses = []
                for r in breakdown_rows:
                    expected = Decimal(str(r.get("expected_amount", r["total"]) or 0))
                    reported_raw = reported_map.get((r["group_slug"], r["method_name"]))
                    reported = Decimal(str(reported_raw)) if reported_raw is not None else None
                    if reported is not None:
                        reported = _normalize_reported_for_expected(expected, reported)
                    reported_amounts.append(float(reported) if reported is not None else None)
                    differences.append(float(reported - expected) if reported is not None else None)
                    statuses.append(
                        _status_from_reported(expected, reported)
                        if reported is not None
                        else _initial_reconciliation_status(r["group_slug"], float(expected))
                    )
                await conn.execute(
                    """
                    INSERT INTO cierre_payment_breakdown (
                        cierre_id, group_slug, method_name, total,
                        gross_inflows_amount, expense_outflows_amount, purchase_outflows_amount,
                        expected_amount, reported_amount, difference_amount,
                        reconciliation_status
                    )
                    SELECT
                        $1,
                        unnest($2::text[]),
                        unnest($3::text[]),
                        unnest($4::numeric[]),
                        unnest($5::numeric[]),
                        unnest($6::numeric[]),
                        unnest($7::numeric[]),
                        unnest($8::numeric[]),
                        unnest($9::numeric[]),
                        unnest($10::numeric[]),
                        unnest($11::text[])
                    """,
                    summary_row["id"],
                    [r["group_slug"] for r in breakdown_rows],
                    [r["method_name"] for r in breakdown_rows],
                    [r["total"] for r in breakdown_rows],
                    [r["gross_inflows_amount"] for r in breakdown_rows],
                    [r["expense_outflows_amount"] for r in breakdown_rows],
                    [r["purchase_outflows_amount"] for r in breakdown_rows],
                    [r["expected_amount"] for r in breakdown_rows],
                    reported_amounts,
                    differences,
                    statuses,
                )

            # GL posting for cierre is intentionally disabled.
            # Revenue is already recorded per-order via _post_order_gl_entry()
            # (source_module='orden') when each POS/table/online order completes.
            # Posting again here (source_module='ventas') would double-count
            # income in the SALES_REVENUE role. The cierre is a cash reconciliation
            # report, not as the GL trigger for revenue recognition.

        return {
            "success": True,
            "data": {
                "id":                   str(summary_row["id"]),
                "accountingPeriodId":   str(period_id),
                "tenantId":             str(tenant_id),
                "periodStart":          period_start.isoformat(),
                "periodEnd":            period_end.isoformat(),
                "periodStartTime":      period_start_time.isoformat() if period_start_time else None,
                "periodEndTime":        period_end_time.isoformat()   if period_end_time   else None,
                "shiftTemplateId":      str(shift_template_id) if shift_template_id else None,
                "totalSales":           preview["totalSales"],
                "minimumCoverIncome":   preview.get("minimumCoverIncome", 0.0),
                "itemsSold":            preview["itemsSold"],
                "totalTips":            preview["totalTips"],
                "totalTipTax":          preview["totalTipTax"],
                "totalCharged":         preview["totalCharged"],
                "tableAdvanceCollections": preview.get("tableAdvanceCollections", 0.0),
                "tableAdvanceApplications": preview.get("tableAdvanceApplications", 0.0),
                "tableAdvanceCover":     preview.get("tableAdvanceCover", 0.0),
                "cashTips":             preview["cashTips"],
                "openingCash":          opening_cash,
                "totalCash":            preview["totalCash"],
                "totalCard":            preview["totalCard"],
                "totalDigital":         preview["totalDigital"],
                "totalCredit":          preview["totalCredit"],
                "gastosEfectivo":       preview["gastosEfectivo"],
                "cashExpected":         preview["cashExpected"],
                "cashCounted":          body.cash_counted,
                "cashDifference":       cash_difference,
                "cashLeftInDrawer":     cash_left,
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
# Payment reconciliation
# ---------------------------------------------------------------------------

def _reconciliation_row_to_dict(row) -> dict:
    expected = row["expected_amount"] if row["expected_amount"] is not None else row["total"]
    return {
        "id": str(row["id"]),
        "cierreId": str(row["cierre_id"]),
        "accountingPeriodId": str(row["accounting_period_id"]),
        "periodStart": row["period_start"].isoformat(),
        "periodEnd": row["period_end"].isoformat(),
        "closedAt": row["closed_at"].isoformat() if row["closed_at"] else None,
        "groupSlug": row["group_slug"],
        "methodName": row["method_name"],
        "total": float(row["total"]),
        "grossInflowsAmount": float(row["gross_inflows_amount"] if row["gross_inflows_amount"] is not None else row["total"]),
        "expenseOutflowsAmount": float(row["expense_outflows_amount"] or 0),
        "purchaseOutflowsAmount": float(row["purchase_outflows_amount"] or 0),
        "expectedAmount": float(expected or 0),
        "reportedAmount": float(row["reported_amount"]) if row["reported_amount"] is not None else None,
        "differenceAmount": float(row["difference_amount"]) if row["difference_amount"] is not None else None,
        "reconciliationStatus": row["reconciliation_status"] or _initial_reconciliation_status(
            row["group_slug"], float(expected or 0),
        ),
        "reconciliationReason": row["reconciliation_reason"],
        "reconciliationNotes": row["reconciliation_notes"],
        "journalEntryId": str(row["journal_entry_id"]) if row["journal_entry_id"] else None,
        "resolvedBy": str(row["resolved_by"]) if row["resolved_by"] else None,
        "resolvedAt": row["resolved_at"].isoformat() if row["resolved_at"] else None,
    }


_RECONCILIATION_SELECT = """
    SELECT
        cpb.id, cpb.cierre_id, cpb.group_slug, cpb.method_name, cpb.total,
        cpb.gross_inflows_amount, cpb.expense_outflows_amount, cpb.purchase_outflows_amount,
        cpb.expected_amount, cpb.reported_amount, cpb.difference_amount,
        cpb.reconciliation_status, cpb.reconciliation_reason,
        cpb.reconciliation_notes, cpb.journal_entry_id, cpb.resolved_by,
        cpb.resolved_at,
        cs.accounting_period_id,
        ap.period_start, ap.period_end, ap.closed_at
    FROM cierre_payment_breakdown cpb
    JOIN closing_summary cs ON cs.id = cpb.cierre_id
    JOIN accounting_period ap ON ap.id = cs.accounting_period_id
"""


async def _fetch_reconciliation_row(conn, tenant_id: UUID, reconciliation_id: UUID):
    return await conn.fetchrow(
        f"""
        {_RECONCILIATION_SELECT}
        WHERE cpb.id = $1
          AND cs.tenant_id = $2
          AND ap.deleted_at IS NULL
          AND cpb.group_slug NOT IN ('cash', 'untracked')
        """,
        reconciliation_id, tenant_id,
    )


async def list_reconciliations(
    request: Request,
    status: Optional[str] = None,
    group_slug: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    cierre_id: Optional[UUID] = None,
) -> dict:
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        conditions = [
            "cs.tenant_id = $1",
            "ap.deleted_at IS NULL",
            "cpb.group_slug NOT IN ('cash', 'untracked')",
        ]
        params: List[Any] = [tenant_id]
        if status:
            if status not in RECONCILIATION_STATUSES:
                raise APIError("Estado de conciliación inválido", status_code=422)
            params.append(status)
            conditions.append(f"COALESCE(cpb.reconciliation_status, 'pending') = ${len(params)}")
        if group_slug:
            params.append(group_slug)
            conditions.append(f"cpb.group_slug = ${len(params)}")
        if date_from:
            params.append(date_from)
            conditions.append(f"ap.period_start >= ${len(params)}")
        if date_to:
            params.append(date_to)
            conditions.append(f"ap.period_end <= ${len(params)}")
        if cierre_id:
            params.append(cierre_id)
            conditions.append(f"cpb.cierre_id = ${len(params)}")

        async with get_db_connection(use_transaction=False) as conn:
            rows = await conn.fetch(
                f"""
                {_RECONCILIATION_SELECT}
                WHERE {' AND '.join(conditions)}
                ORDER BY ap.closed_at DESC, cpb.group_slug, cpb.method_name
                """,
                *params,
            )

        data = [_reconciliation_row_to_dict(row) for row in rows]
        summary = {
            "pending": sum(1 for r in data if r["reconciliationStatus"] in {"pending", "needs_review"}),
            "withDifference": sum(1 for r in data if (r["differenceAmount"] or 0) != 0),
            "totalExpected": sum(r["expectedAmount"] for r in data),
            "totalDifference": sum(r["differenceAmount"] or 0 for r in data),
        }
        return {"success": True, "data": data, "summary": summary}

    except (AuthenticationError, APIError):
        raise
    except Exception as exc:
        logger.error(f"Error in list_reconciliations: {exc}")
        raise APIError(f"Error in list_reconciliations: {exc}", status_code=500)


async def get_reconciliation(request: Request, reconciliation_id: UUID) -> dict:
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection(use_transaction=False) as conn:
            row = await _fetch_reconciliation_row(conn, tenant_id, reconciliation_id)
        if not row:
            raise APIError("Conciliación no encontrada", status_code=404)
        return {"success": True, "data": _reconciliation_row_to_dict(row)}

    except (AuthenticationError, APIError):
        raise
    except Exception as exc:
        logger.error(f"Error in get_reconciliation: {exc}")
        raise APIError(f"Error in get_reconciliation: {exc}", status_code=500)


async def update_reconciliation_reported(
    request: Request,
    reconciliation_id: UUID,
    body: CierreReconciliationReportedUpdate,
) -> dict:
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                row = await _fetch_reconciliation_row(conn, tenant_id, reconciliation_id)
                if not row:
                    raise APIError("Conciliación no encontrada", status_code=404)
                if row["journal_entry_id"]:
                    raise APIError(
                        "Esta conciliación ya tiene un asiento enlazado. Anula o revisa el asiento antes de cambiar el monto.",
                        status_code=409,
                    )
                expected = Decimal(str(row["expected_amount"] if row["expected_amount"] is not None else row["total"]))
                reported = _normalize_reported_for_expected(expected, Decimal(str(body.reported_amount)))
                status = _status_from_reported(expected, reported)
                updated = await conn.fetchrow(
                    """
                    UPDATE cierre_payment_breakdown
                    SET reported_amount = $2,
                        difference_amount = $3,
                        reconciliation_status = $4,
                        reconciliation_notes = COALESCE($5, reconciliation_notes),
                        reconciliation_reason = NULL,
                        resolved_by = NULL,
                        resolved_at = NULL
                    WHERE id = $1
                    RETURNING *
                    """,
                    reconciliation_id,
                    float(reported),
                    float(reported - expected),
                    status,
                    body.notes,
                )
                full = await _fetch_reconciliation_row(conn, tenant_id, updated["id"])

        return {"success": True, "data": _reconciliation_row_to_dict(full)}

    except (AuthenticationError, APIError):
        raise
    except Exception as exc:
        logger.error(f"Error in update_reconciliation_reported: {exc}")
        raise APIError(f"Error in update_reconciliation_reported: {exc}", status_code=500)


async def _create_reconciliation_journal_entry(
    conn,
    tenant_id: UUID,
    user_id: Optional[UUID],
    row,
    reason: str,
    notes: Optional[str],
):
    diff = Decimal(str(row["difference_amount"] or 0))
    if diff == 0:
        return None

    no_auto_entry_reasons = {"timing", "method_misclassified", "duplicate"}
    if reason in no_auto_entry_reasons:
        return None

    amount = abs(diff)
    payment_account = await resolve_payment_account(
        conn, tenant_id, row["group_slug"], source="reconciliation"
    )
    debit_account = None
    credit_account = None
    if reason == "commission":
        debit_account = await resolve_account(
            conn, tenant_id, AccountRole.BANK_FEES_EXPENSE, source="reconciliation"
        )
        credit_account = payment_account
    elif reason == "missing_sale":
        debit_account = payment_account
        credit_account = await resolve_account(
            conn, tenant_id, AccountRole.SALES_REVENUE, source="reconciliation"
        )
    elif reason == "client_balance":
        debit_account = payment_account
        credit_account = await resolve_account(
            conn, tenant_id, AccountRole.CUSTOMER_ADVANCES, source="reconciliation"
        )
    elif reason == "real_surplus":
        debit_account = payment_account
        credit_account = await resolve_account(
            conn, tenant_id, AccountRole.OTHER_INCOME, source="reconciliation"
        )
    elif reason == "real_shortage":
        debit_account = await resolve_account(
            conn, tenant_id, AccountRole.ACCOUNTS_RECEIVABLE, source="reconciliation"
        )
        credit_account = payment_account
    elif reason == "other":
        if diff > 0:
            debit_account = payment_account
            credit_account = await resolve_account(
                conn, tenant_id, AccountRole.OTHER_INCOME, source="reconciliation"
            )
        else:
            debit_account = await resolve_account(
                conn, tenant_id, AccountRole.BANK_FEES_EXPENSE, source="reconciliation"
            )
            credit_account = payment_account
    else:
        return None

    entry_date = row["period_end"]
    description = (
        f"Conciliación {row['method_name']} — "
        f"{row['period_start'].isoformat()} a {row['period_end'].isoformat()}"
    )
    if notes:
        description = f"{description}: {notes[:180]}"

    entry_row = await conn.fetchrow(
        """
        INSERT INTO tenant_journal_entries
            (tenant_id, entry_date, period_year, period_month,
             description, source_module, source_id, status,
             total_debit, total_credit, created_by, pending_review)
        VALUES ($1, $2, $3, $4, $5, 'arqueo', $6, 'draft', $7, $8, $9, true)
        RETURNING id
        """,
        tenant_id,
        entry_date,
        entry_date.year,
        entry_date.month,
        description,
        row["id"],
        float(amount),
        float(amount),
        user_id,
    )
    entry_id = entry_row["id"]
    await conn.execute(
        """
        INSERT INTO tenant_journal_lines
            (journal_entry_id, account_id, debit, credit, description, line_order)
        VALUES
            ($1, $2, $3, 0, $5, 0),
            ($1, $4, 0, $3, $5, 1)
        """,
        entry_id,
        debit_account.id,
        float(amount),
        credit_account.id,
        description,
    )
    return entry_id


async def resolve_reconciliation(
    request: Request,
    reconciliation_id: UUID,
    body: CierreReconciliationResolve,
) -> dict:
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")
        if body.reason not in RECONCILIATION_REASONS:
            raise APIError("Motivo de conciliación inválido", status_code=422)

        async with get_db_connection() as conn:
            async with conn.transaction():
                row = await _fetch_reconciliation_row(conn, tenant_id, reconciliation_id)
                if not row:
                    raise APIError("Conciliación no encontrada", status_code=404)
                if row["reported_amount"] is None:
                    raise APIError("Primero registra el monto reportado", status_code=422)

                journal_entry_id = row["journal_entry_id"]
                if body.create_journal_entry and not journal_entry_id:
                    journal_entry_id = await _create_reconciliation_journal_entry(
                        conn, tenant_id, user_id, row, body.reason, body.notes,
                    )

                await conn.execute(
                    """
                    UPDATE cierre_payment_breakdown
                    SET reconciliation_status = 'resolved',
                        reconciliation_reason = $2,
                        reconciliation_notes = COALESCE($3, reconciliation_notes),
                        journal_entry_id = COALESCE($4, journal_entry_id),
                        resolved_by = $5,
                        resolved_at = NOW()
                    WHERE id = $1
                    """,
                    reconciliation_id,
                    body.reason,
                    body.notes,
                    journal_entry_id,
                    user_id,
                )
                updated = await _fetch_reconciliation_row(conn, tenant_id, reconciliation_id)

        return {"success": True, "data": _reconciliation_row_to_dict(updated)}

    except (AuthenticationError, APIError):
        raise
    except Exception as exc:
        logger.error(f"Error in resolve_reconciliation: {exc}")
        raise APIError(f"Error in resolve_reconciliation: {exc}", status_code=500)


# ---------------------------------------------------------------------------
# GET /cierre
# ---------------------------------------------------------------------------

_CIERRE_SUMMARY_COLUMNS = """
    cs.id, cs.accounting_period_id, cs.tenant_id,
    ap.period_start, ap.period_end, ap.period_start_time, ap.period_end_time, ap.closed_at,
    ap.shift_template_id,
    tst.name AS shift_template_name,
    cs.total_sales, cs.items_sold,
    cs.total_tips, cs.total_tip_tax, cs.cash_tips,
    cs.total_cash, cs.total_card, cs.total_digital, cs.total_credit,
    cs.gastos_efectivo, COALESCE(cs.cash_purchases, 0) AS cash_purchases,
    cs.opening_cash, cs.cash_expected, cs.cash_counted, cs.cash_difference,
    cs.cash_left_in_drawer, cs.notes,
    (
        SELECT COUNT(*)
        FROM cierre_payment_breakdown cpb
        WHERE cpb.cierre_id = cs.id
          AND cpb.group_slug NOT IN ('cash', 'untracked')
          AND cpb.reconciliation_status IN ('pending', 'needs_review')
    ) AS reconciliation_pending_count,
    (
        SELECT COALESCE(SUM(cpb.difference_amount), 0)
        FROM cierre_payment_breakdown cpb
        WHERE cpb.cierre_id = cs.id
          AND cpb.group_slug NOT IN ('cash', 'untracked')
          AND cpb.difference_amount IS NOT NULL
    ) AS reconciliation_difference_total
"""

_CIERRE_SUMMARY_FROM = """
    FROM closing_summary cs
    JOIN accounting_period ap ON ap.id = cs.accounting_period_id
    LEFT JOIN tenant_shift_templates tst ON tst.id = ap.shift_template_id
"""


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
            timezone_name = await resolve_tenant_timezone(conn, tenant_id)
            params.append(timezone_name)
            timezone_param = f"${len(params)}"
            open_rows = await conn.fetch(
                """
                SELECT
                    cso.id, cso.opening_cash, cso.opening_breakdown, cso.opened_at,
                    cso.opened_by_user_id, cso.shift_template_id,
                    cso.period_start, cso.period_end,
                    cso.period_start_time, cso.period_end_time,
                    tst.name AS shift_template_name
                FROM cash_shift_openings cso
                LEFT JOIN tenant_shift_templates tst ON tst.id = cso.shift_template_id
                WHERE cso.tenant_id = $1 AND cso.status = 'open'
                ORDER BY cso.opened_at DESC
                """,
                tenant_id,
            )
            rows = await conn.fetch(
                f"""
                SELECT
                    {_CIERRE_SUMMARY_COLUMNS}
                {_CIERRE_SUMMARY_FROM}
                WHERE cs.tenant_id = $1
                  AND ap.deleted_at IS NULL
                {date_filter}
                ORDER BY
                    COALESCE(
                        ap.period_start_time,
                        ap.period_start::timestamp AT TIME ZONE {timezone_param}
                    ) DESC,
                    ap.closed_at DESC
                """,
                *params,
            )

        data = [_open_shift_list_row_to_dict(row) for row in open_rows]
        data.extend(_row_to_dict(row) for row in rows)
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
                f"""
                SELECT
                    {_CIERRE_SUMMARY_COLUMNS}
                {_CIERRE_SUMMARY_FROM}
                WHERE cs.id = $1 AND cs.tenant_id = $2 AND ap.deleted_at IS NULL
                """,
                cierre_id, tenant_id,
            )

            if not row:
                raise APIError("Cierre no encontrado", status_code=404)

            breakdown_rows = await conn.fetch(
                """
                SELECT
                    id, group_slug, method_name, total, expected_amount,
                    gross_inflows_amount, expense_outflows_amount, purchase_outflows_amount,
                    reported_amount, difference_amount, reconciliation_status,
                    reconciliation_reason, reconciliation_notes, journal_entry_id,
                    resolved_by, resolved_at
                FROM cierre_payment_breakdown
                WHERE cierre_id = $1
                ORDER BY group_slug, method_name
                """,
                row["id"],
            )
            breakdown = [
                {
                    "id":         str(r["id"]),
                    "groupSlug":  r["group_slug"],
                    "methodName": r["method_name"],
                    "total":      float(r["total"]),
                    "grossInflowsAmount": float(r["gross_inflows_amount"] if r["gross_inflows_amount"] is not None else r["total"]),
                    "expenseOutflowsAmount": float(r["expense_outflows_amount"] or 0),
                    "purchaseOutflowsAmount": float(r["purchase_outflows_amount"] or 0),
                    "expectedAmount": float(r["expected_amount"] if r["expected_amount"] is not None else r["total"]),
                    "reportedAmount": float(r["reported_amount"]) if r["reported_amount"] is not None else None,
                    "differenceAmount": float(r["difference_amount"]) if r["difference_amount"] is not None else None,
                    "reconciliationStatus": r["reconciliation_status"],
                    "reconciliationReason": r["reconciliation_reason"],
                    "reconciliationNotes": r["reconciliation_notes"],
                    "journalEntryId": str(r["journal_entry_id"]) if r["journal_entry_id"] else None,
                    "resolvedBy": str(r["resolved_by"]) if r["resolved_by"] else None,
                    "resolvedAt": r["resolved_at"].isoformat() if r["resolved_at"] else None,
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

            # GL void not needed: no ventas GL entry is created on cierre.
            # Revenue GL entries (source_module='orden') are never voided on
            # cierre delete — they remain as the permanent per-order record.

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
# DELETE /cierre/open-shift/{opening_id}  — cancel open shift (no cierre yet)
# ---------------------------------------------------------------------------

async def delete_open_shift(request: Request, opening_id: UUID) -> dict:
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection(use_transaction=True) as conn:
            row = await conn.fetchrow(
                """
                SELECT id, status, accounting_period_id
                FROM cash_shift_openings
                WHERE id = $1 AND tenant_id = $2
                """,
                opening_id, tenant_id,
            )
            if not row:
                raise APIError("Apertura no encontrada", status_code=404)
            if row["status"] != "open" or row["accounting_period_id"] is not None:
                raise APIError(
                    "Solo se puede cancelar una apertura de turno abierta sin cierre",
                    status_code=409,
                )

            await conn.execute(
                "DELETE FROM cash_shift_openings WHERE id = $1 AND tenant_id = $2",
                opening_id, tenant_id,
            )

        return {"success": True, "data": None}

    except (AuthenticationError, APIError):
        raise
    except Exception as exc:
        logger.error(f"Error in delete_open_shift: {exc}")
        raise APIError(f"Error in delete_open_shift: {exc}", status_code=500)


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
                f"""
                SELECT
                    {_CIERRE_SUMMARY_COLUMNS}
                {_CIERRE_SUMMARY_FROM}
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
            "totalTips":      sum(r["totalTips"]       for r in daily),
            "totalTipTax":    sum(r["totalTipTax"]     for r in daily),
            "cashTips":       sum(r["cashTips"]        for r in daily),
            "totalCharged":   sum(r["totalCharged"]    for r in daily),
            "totalCash":      sum(r["totalCash"]       for r in daily),
            "totalCard":      sum(r["totalCard"]       for r in daily),
            "totalDigital":   sum(r["totalDigital"]    for r in daily),
            "totalCredit":    sum(r["totalCredit"]     for r in daily),
            "gastosEfectivo": sum(r["gastosEfectivo"]  for r in daily),
            "cashPurchases":   sum(r["cashPurchases"]   for r in daily),
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
                f"""
                SELECT
                    {_CIERRE_SUMMARY_COLUMNS}
                {_CIERRE_SUMMARY_FROM}
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

def _row_value(row, key: str, default=None):
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def _row_to_dict(row) -> dict:
    return {
        "id":                   str(row["id"]),
        "status":               "closed",
        "accountingPeriodId":   str(row["accounting_period_id"]),
        "tenantId":             str(row["tenant_id"]),
        "periodStart":          row["period_start"].isoformat(),
        "periodEnd":            row["period_end"].isoformat(),
        "periodStartTime":      row["period_start_time"].isoformat() if row["period_start_time"] else None,
        "periodEndTime":        row["period_end_time"].isoformat()   if row["period_end_time"]   else None,
        "shiftTemplateId":      str(row["shift_template_id"]) if row["shift_template_id"] else None,
        "shiftTemplateName":    row["shift_template_name"],
        "totalSales":           float(row["total_sales"]),
        "itemsSold":            int(row["items_sold"]),
        "totalTips":            float(row["total_tips"] or 0),
        "totalTipTax":          float(row["total_tip_tax"] or 0),
        "cashTips":             float(row["cash_tips"] or 0),
        "totalCharged":         float(row["total_sales"]) + tip_settlement_total(
            float(row["total_tips"] or 0),
            float(row["total_tip_tax"] or 0),
        ),
        "totalCash":            float(row["total_cash"]),
        "totalCard":            float(row["total_card"]),
        "totalDigital":         float(row["total_digital"]),
        "totalCredit":          float(row["total_credit"]),
        "gastosEfectivo":       float(row["gastos_efectivo"]),
        "cashPurchases":        float(_row_value(row, "cash_purchases", 0) or 0),
        "openingCash":          float(row["opening_cash"] or 0),
        "cashExpected":         float(row["cash_expected"]),
        "cashCounted":          float(row["cash_counted"]),
        "cashDifference":       float(row["cash_difference"]),
        "cashLeftInDrawer":     float(row["cash_left_in_drawer"]) if row["cash_left_in_drawer"] is not None else None,
        "reconciliationPendingCount": int(_row_value(row, "reconciliation_pending_count", 0) or 0),
        "reconciliationDifferenceTotal": float(_row_value(row, "reconciliation_difference_total", 0) or 0),
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

            await check_plan_quota_period(
                conn, tenant_id, "accounting_period_closes_per_period"
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

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
from app.models.cierre import CierreCashSettingsUpdate, CierreCreate, OpenShiftCreate
from app.services.tip_tax_service import tip_settlement_total

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
    "customer_wallet": "2810",  # Anticipos clientes (api#369; tenant override in _post_order_gl_entry)
    "table_session_advance": "2810",  # Anticipos recibidos aplicados a consumo minimo de mesa
}

INGRESOS_CODE   = "4175"   # Servicios de restaurante y similares
COGS_CODE       = "6135"   # Costo de ventas
INVENTARIO_CODE = "1435"   # Inventarios — materia prima y suministros


def _tip_gl_amounts(
    tip_amount: Decimal,
    tip_tax_amount: Decimal,
    tax_config: Dict[str, Any],
) -> tuple:
    """
    Return (settlement_debit, net_tip_revenue, tip_tax_credit) for GL posting.
    Mirrors additive vs extractive tip tax from tenant_tax_config.
    """
    tip_amt = Decimal(str(tip_amount or 0))
    tip_tax = Decimal(str(tip_tax_amount or 0))
    if tip_amt <= 0 and tip_tax <= 0:
        return Decimal("0"), Decimal("0"), Decimal("0")

    tip_tax_additive = False
    if tip_tax > 0:
        if tax_config.get("inc_applicable"):
            tip_tax_additive = not tax_config.get("inc_included_in_price", True)
        elif tax_config.get("iva_applicable"):
            tip_tax_additive = not tax_config.get("iva_included_in_price", False)

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
    tax_code = None
    if tax_config.get("inc_applicable"):
        tax_code = str(tax_config["inc_gl_account_code"])
    elif tax_config.get("iva_applicable"):
        tax_code = str(tax_config["iva_gl_account_code"])
    if not tax_code:
        return None
    tax_row = await conn.fetchrow(
        "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = $2 AND is_active = true",
        tenant_id, tax_code,
    )
    return tax_row["id"] if tax_row else None


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

    DR  [payment account]    debit_total   (1105 Caja / 1110 Bancos / 1305 Clientes)
    CR  4175 Ingresos        product net + dedicated tip line when tip > 0
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

    async def resolve_debit_code(method: str, method_id: Optional[UUID]) -> str:
        debit_code = None
        if method_id:
            pm_row = await conn.fetchrow(
                """SELECT COALESCE(pm.gl_account_code, pmg.gl_account_code) AS code
                   FROM payment_methods pm
                   JOIN payment_method_groups pmg ON pm.group_id = pmg.id
                   WHERE pm.id = $1""",
                method_id,
            )
            if pm_row and pm_row["code"]:
                debit_code = pm_row["code"]
        if not debit_code:
            debit_code = _SLUG_DEBIT_CODE.get(method or "", "1105")
        if method == "customer_wallet":
            liability_row = await conn.fetchrow(
                """
                SELECT customer_wallet_liability_gl_code
                FROM tenant_public_profiles
                WHERE tenant_id = $1
                """,
                tenant_id,
            )
            if liability_row and liability_row["customer_wallet_liability_gl_code"]:
                debit_code = str(liability_row["customer_wallet_liability_gl_code"])
        return str(debit_code)

    async def resolve_debit_account(code: str):
        debit_acct = await conn.fetchrow(
            "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = $2 AND is_active = true",
            tenant_id,
            code,
        )
        if not debit_acct:
            logger.warning(
                f"[GL] Debit account {code} not found for tenant {tenant_id} — "
                f"skip GL post for order {order_id}"
            )
            return None
        return debit_acct

    # ── Resolve debit account: specific method → group → slug fallback ────
    debit_code = await resolve_debit_code(payment_method, payment_method_id)
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
                        "code": await resolve_debit_code(
                            split.get("payment_method") or "",
                            split_method_id,
                        ),
                    }
                )

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

    # ── Calculate taxes per category ──────────────────────────────────────
    standard_tax     = Decimal("0")
    liquor_tax       = Decimal("0")
    standard_acct_id = None
    liquor_acct_id   = None
    standard_is_additive = False

    if tax_config.get("inc_applicable") and standard_subtotal > 0:
        rate = Decimal(str(tax_config["inc_rate"]))
        tax_code = str(tax_config["inc_gl_account_code"])
        if tax_config.get("inc_included_in_price", True):
            # Extractive: price already includes INC
            standard_tax = standard_subtotal - (standard_subtotal / (1 + rate))
        else:
            # Additive: INC charged on top of base price
            standard_tax = standard_subtotal * rate
            standard_is_additive = True
        tax_row = await conn.fetchrow(
            "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = $2 AND is_active = true",
            tenant_id, tax_code,
        )
        if tax_row:
            standard_acct_id = tax_row["id"]

    elif tax_config.get("iva_applicable") and standard_subtotal > 0:
        rate = Decimal(str(tax_config["iva_rate"]))
        tax_code = str(tax_config["iva_gl_account_code"])
        if tax_config.get("iva_included_in_price", False):
            standard_tax = standard_subtotal - (standard_subtotal / (1 + rate))
        else:
            standard_tax = standard_subtotal * rate
            standard_is_additive = True
        tax_row = await conn.fetchrow(
            "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = $2 AND is_active = true",
            tenant_id, tax_code,
        )
        if tax_row:
            standard_acct_id = tax_row["id"]

    if tax_config.get("liquor_tax_applicable") and liquor_subtotal > 0:
        rate = Decimal(str(tax_config["liquor_tax_rate"]))
        tax_code = str(tax_config["liquor_tax_gl_account_code"])
        liquor_tax = liquor_subtotal * rate  # IVA licores — always additive (external VAT)
        tax_row = await conn.fetchrow(
            "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = $2 AND is_active = true",
            tenant_id, tax_code,
        )
        if tax_row:
            liquor_acct_id = tax_row["id"]

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
                split_acct = await resolve_debit_account(split["code"])
                if not split_acct:
                    return
                split_debit_lines.append(
                    {
                        "account_id": split_acct["id"],
                        "amount": debit_amount,
                        "payment_method": split["payment_method"],
                    }
                )
        else:
            debit_acct = await resolve_debit_account(debit_code)
            if not debit_acct:
                return
    advance_acct = None
    if advance_debit > 0:
        advance_acct = await conn.fetchrow(
            "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = $2 AND is_active = true",
            tenant_id, _SLUG_DEBIT_CODE["table_session_advance"],
        )
        if not advance_acct:
            logger.warning(
                f"[GL] Advance account 2810 not found for tenant {tenant_id} — "
                f"skip GL post for order {order_id}"
            )
            return

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
                advance_acct["id"],
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
                debit_acct["id"],
                float(payment_debit),
                description,
                line_order,
            )
            line_order += 1

        # Credit line — product net revenue to 4175
        await conn.execute(
            """INSERT INTO tenant_journal_lines
                   (journal_entry_id, account_id, debit, credit, description, line_order)
               VALUES ($1, $2, 0, $3, $4, $5)""",
            entry_id, ingresos_acct["id"], float(product_net_revenue),
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
                entry_id, ingresos_acct["id"], float(tip_net_revenue),
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
    ingresos_acct = await conn.fetchrow(
        "SELECT id FROM tenant_accounts WHERE tenant_id = $1 AND code = $2 AND is_active = true",
        tenant_id, INGRESOS_CODE,
    )
    if not debit_acct or not ingresos_acct:
        logger.warning(
            f"[GL] Deferred tip for order {order_id}: missing debit/ingresos account — skip"
        )
        return

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
            entry_id, debit_acct["id"], settlement_f, tip_description, line_order,
        )
        line_order += 1
        await conn.execute(
            """INSERT INTO tenant_journal_lines
                   (journal_entry_id, account_id, debit, credit, description, line_order)
               VALUES ($1, $2, 0, $3, $4, $5)""",
            entry_id, ingresos_acct["id"], float(tip_net_revenue),
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
):
    """
    Cash expenses: date-only arqueos use transaction_date (business day).
    Shift windows use created_at (transaction_date has no time component).
    """
    p2 = f"${param_offset}"
    p3 = f"${param_offset + 1}"
    if period_start_time and period_end_time:
        sql = f"AND created_at >= {p2} AND created_at <= {p3}"
        return sql, [period_start_time, period_end_time]
    sql = f"AND transaction_date >= {p2} AND transaction_date <= {p3}"
    return sql, [period_start, period_end]


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
    cash_tips: float,
    total_sales: float,
    gastos_efectivo: float,
) -> float:
    """Expected drawer cash: opening float + cash received − cash expenses.

    When total_cash already includes tip settlement (all-cash), do not add
    cash_tips again. Otherwise cash_tips is additive (split-pay / card tips).
    """
    if total_cash >= total_sales + cash_tips:
        return opening_cash + total_cash - gastos_efectivo
    return opening_cash + total_cash + cash_tips - gastos_efectivo


def _sum_advance_bucket(bucket: Dict[str, float]) -> float:
    return sum(float(total or 0.0) for total in bucket.values())


def _advance_audit_totals(advance_totals: Dict[str, Dict[str, float]]) -> Dict[str, float]:
    """Audit values used by cierre to explain table-session advance reconciliation."""
    return {
        "tableAdvanceCollections": _sum_advance_bucket(advance_totals.get("collections", {})),
        "tableAdvanceApplications": _sum_advance_bucket(advance_totals.get("applications", {})),
        "tableAdvanceCover": float(advance_totals.get("cover", {}).get("total", 0.0)),
    }


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
    total_tips = float(tips_row["total_tips"])
    total_tip_tax = float(tips_row["total_tip_tax"])
    cash_tips = float(cash_tips_row["cash_tips"])
    total_charged = float(sales_row["total_sales"]) + minimum_cover_income + tip_settlement_total(
        total_tips, total_tip_tax,
    )
    cash_expected = _compute_cash_expected(
        float(opening_cash),
        total_cash,
        cash_tips,
        float(sales_row["total_sales"]),
        gastos_efectivo,
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
                    },
                }

            return {"success": True, "data": _open_shift_row_to_dict(row)}

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
                    gastos_efectivo, opening_cash, cash_expected, cash_counted, cash_difference,
                    cash_left_in_drawer, notes
                ) VALUES (
                    $1, $2,
                    $3, $4,
                    $5, $6, $7,
                    $8, $9, $10, $11,
                    $12, $13, $14, $15, $16,
                    $17, $18
                )
                RETURNING id, created_at
                """,
                period_id, tenant_id,
                preview["totalSales"], preview["itemsSold"],
                preview["totalTips"], preview["totalTipTax"], preview["cashTips"],
                preview["totalCash"], preview["totalCard"],
                preview["totalDigital"], preview["totalCredit"],
                preview["gastosEfectivo"], opening_cash, preview["cashExpected"],
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

            # GL posting for cierre is intentionally disabled.
            # Revenue is already recorded per-order via _post_order_gl_entry()
            # (source_module='orden') when each POS/table/online order completes.
            # Posting again here (source_module='ventas') would double-count
            # income in account 4175. The cierre serves as a cash reconciliation
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
    cs.gastos_efectivo, cs.opening_cash, cs.cash_expected, cs.cash_counted, cs.cash_difference,
    cs.cash_left_in_drawer, cs.notes
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
        "openingCash":          float(row["opening_cash"] or 0),
        "cashExpected":         float(row["cash_expected"]),
        "cashCounted":          float(row["cash_counted"]),
        "cashDifference":       float(row["cash_difference"]),
        "cashLeftInDrawer":     float(row["cash_left_in_drawer"]) if row["cash_left_in_drawer"] is not None else None,
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

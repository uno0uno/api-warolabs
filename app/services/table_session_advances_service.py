"""
Session-scoped advances for minimum consumption / cover.

Issue: https://github.com/uno0uno/warocol.com/issues/1370
"""
from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import Request

from app.core.exceptions import APIError, AuthenticationError, NotFoundError
from app.core.middleware import require_valid_session
from app.database import get_db_connection
from app.services.customer_wallet_service import (
    WALLET_PAYMENT_SLUG,
    _post_two_line_gl,
    _resolve_liability_account,
    _resolve_payment_debit_account,
)
from app.services.account_role_service import (
    AccountRole,
    resolve_account,
    resolve_tax_account,
)

logger = logging.getLogger(__name__)

ALLOWED_ADVANCE_TENDERS = {"cash", "card", "digital"}
TABLE_SESSION_ADVANCE_PAYMENT_SLUG = "table_session_advance"
ADVANCE_RECEIVE_SOURCE = "table_session_advance_receive"
ADVANCE_VOID_SOURCE = "table_session_advance_void"
ADVANCE_COVER_SOURCE = "table_session_advance_cover"


def _amount_decimal(amount: Decimal | float | int | str) -> Decimal:
    try:
        value = Decimal(str(amount))
    except Exception as exc:
        raise APIError("El monto del anticipo no es válido", status_code=400) from exc
    if value <= 0:
        raise APIError("El monto del anticipo debe ser mayor a cero", status_code=400)
    return value.quantize(Decimal("0.01"))


def _validate_advance_tender(payment_method: str) -> None:
    if payment_method == WALLET_PAYMENT_SLUG:
        raise APIError("El anticipo de mesa no usa billetera de cliente", status_code=400)
    if payment_method not in ALLOWED_ADVANCE_TENDERS:
        raise APIError(
            "Use cash, card o digital como método del anticipo",
            status_code=400,
            details={"code": "payment_method_invalid"},
        )


async def _assert_payment_selection(
    conn,
    tenant_id: UUID,
    payment_method: str,
    payment_method_id: Optional[UUID],
) -> None:
    group_row = await conn.fetchrow(
        """
        SELECT id
        FROM payment_method_groups
        WHERE slug = $1
          AND is_active = true
          AND (tenant_id IS NULL OR tenant_id = $2)
        """,
        payment_method,
        tenant_id,
    )
    if not group_row:
        raise APIError(
            f"Método de pago '{payment_method}' no es válido para este restaurante",
            status_code=400,
            details={"code": "payment_method_invalid"},
        )
    if not payment_method_id:
        return
    method_row = await conn.fetchrow(
        """
        SELECT id
        FROM payment_methods
        WHERE id = $1
          AND tenant_id = $2
          AND group_id = $3
          AND is_active = true
        """,
        payment_method_id,
        tenant_id,
        group_row["id"],
    )
    if not method_row:
        raise APIError(
            "El método seleccionado no pertenece al grupo elegido",
            status_code=400,
            details={"code": "payment_method_id_invalid"},
        )


def _row_get(row, key: str, default=None):
    try:
        return row[key]
    except (KeyError, TypeError):
        return default


async def _get_open_session(conn, tenant_id: UUID, table_id: UUID, *, lock: bool = False):
    table_row = await conn.fetchrow(
        """
        SELECT id
        FROM tables
        WHERE id = $1
          AND tenant_id = $2
          AND is_active = true
        """,
        table_id,
        tenant_id,
    )
    if not table_row:
        raise NotFoundError("Table not found")
    lock_sql = "FOR UPDATE" if lock else ""
    session_row = await conn.fetchrow(
        f"""
        SELECT id, table_id
        FROM table_sessions
        WHERE table_id = $1
          AND tenant_id = $2
          AND closed_at IS NULL
        {lock_sql}
        """,
        table_id,
        tenant_id,
    )
    if not session_row:
        raise NotFoundError("No open session for this table")
    return session_row


def _serialize_advance(row) -> Dict[str, Any]:
    amount = float(row["amount_cop"] or 0)
    applied = float(_row_get(row, "applied_amount_cop", 0) or 0)
    return {
        "id": str(row["id"]),
        "table_session_id": str(row["table_session_id"]),
        "amount_cop": amount,
        "applied_amount_cop": applied,
        "available_amount_cop": max(amount - applied, 0),
        "payment_method": row["payment_method"],
        "payment_method_id": str(row["payment_method_id"]) if row["payment_method_id"] else None,
        "journal_entry_id": str(row["journal_entry_id"]) if row["journal_entry_id"] else None,
        "void_journal_entry_id": (
            str(row["void_journal_entry_id"]) if _row_get(row, "void_journal_entry_id") else None
        ),
        "status": row["status"],
        "notes": _row_get(row, "notes"),
        "void_reason": _row_get(row, "void_reason"),
        "voided_at": row["voided_at"].isoformat() if _row_get(row, "voided_at") else None,
        "applied_at": row["applied_at"].isoformat() if _row_get(row, "applied_at") else None,
        "created_at": row["created_at"].isoformat() if _row_get(row, "created_at") else None,
    }


def _advance_available_amount(row) -> Decimal:
    amount = Decimal(str(row["amount_cop"] or 0))
    applied = Decimal(str(_row_get(row, "applied_amount_cop", 0) or 0))
    return max(amount - applied, Decimal("0"))


def _advance_totals(rows: List[Any]) -> Dict[str, float]:
    active = sum(
        float(_advance_available_amount(row))
        for row in rows
        if row["status"] == "active"
    )
    applied = sum(
        float(_row_get(row, "applied_amount_cop", 0) or 0)
        for row in rows
        if row["status"] == "active"
    )
    voided = sum(
        float(row["amount_cop"] or 0)
        for row in rows
        if row["status"] == "voided"
    )
    return {
        "active_total_cop": active,
        "available_total_cop": active,
        "applied_total_cop": applied,
        "voided_total_cop": voided,
    }


async def _fetch_advances(conn, tenant_id: UUID, table_session_id: UUID) -> List[Any]:
    return await conn.fetch(
        """
        SELECT
            id,
            table_session_id,
            amount_cop,
            payment_method,
            payment_method_id,
            journal_entry_id,
            void_journal_entry_id,
            COALESCE(applied_amount_cop, 0) AS applied_amount_cop,
            applied_at,
            status,
            notes,
            void_reason,
            voided_at,
            created_at
        FROM table_session_advances
        WHERE tenant_id = $1
          AND table_session_id = $2
        ORDER BY created_at DESC, id DESC
        """,
        tenant_id,
        table_session_id,
    )


async def get_session_advances_payload(
    conn,
    tenant_id: UUID,
    table_session_id: UUID,
) -> Dict[str, Any]:
    rows = await _fetch_advances(conn, tenant_id, table_session_id)
    return {
        "advances": [_serialize_advance(row) for row in rows],
        "advance_totals": _advance_totals(rows),
    }


async def get_available_advance_total(
    conn,
    tenant_id: UUID,
    table_session_id: UUID,
) -> Decimal:
    value = await conn.fetchval(
        """
        SELECT COALESCE(SUM(amount_cop - COALESCE(applied_amount_cop, 0)), 0)
        FROM table_session_advances
        WHERE tenant_id = $1
          AND table_session_id = $2
          AND status = 'active'
        """,
        tenant_id,
        table_session_id,
    )
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


async def apply_session_advances_for_close(
    conn,
    tenant_id: UUID,
    table_session_id: UUID,
    amount_cop: Decimal,
    order_ids: List[UUID],
) -> Decimal:
    amount_to_apply = Decimal(str(amount_cop or 0)).quantize(Decimal("0.01"))
    if amount_to_apply <= 0:
        return Decimal("0")

    rows = await conn.fetch(
        """
        SELECT id, amount_cop, COALESCE(applied_amount_cop, 0) AS applied_amount_cop
        FROM table_session_advances
        WHERE tenant_id = $1
          AND table_session_id = $2
          AND status = 'active'
          AND amount_cop > COALESCE(applied_amount_cop, 0)
        ORDER BY created_at, id
        FOR UPDATE
        """,
        tenant_id,
        table_session_id,
    )

    applied_total = Decimal("0")
    remaining = amount_to_apply
    for row in rows:
        available = _advance_available_amount(row)
        if available <= 0:
            continue
        applied = min(available, remaining)
        if applied <= 0:
            break
        await conn.execute(
            """
            UPDATE table_session_advances
            SET applied_amount_cop = COALESCE(applied_amount_cop, 0) + $1,
                applied_at = COALESCE(applied_at, now()),
                applied_order_ids = $2::uuid[],
                updated_at = now()
            WHERE id = $3
              AND tenant_id = $4
            """,
            applied,
            order_ids,
            row["id"],
            tenant_id,
        )
        applied_total += applied
        remaining -= applied
        if remaining <= 0:
            break

    return applied_total.quantize(Decimal("0.01"))


def _standard_cover_gl_amounts(amount: Decimal, tax_config: Dict[str, Any]) -> tuple:
    settlement = Decimal(str(amount or 0)).quantize(Decimal("0.01"))
    tax_amount = Decimal("0")
    tax_kind = None
    if settlement <= 0:
        return settlement, settlement, tax_amount, tax_kind
    if tax_config.get("inc_applicable"):
        rate = Decimal(str(tax_config["inc_rate"]))
        tax_kind = "inc"
        tax_amount = settlement - (settlement / (1 + rate))
    elif tax_config.get("iva_applicable"):
        rate = Decimal(str(tax_config["iva_rate"]))
        tax_kind = "iva"
        tax_amount = settlement - (settlement / (1 + rate))
    return settlement, settlement - tax_amount, tax_amount, tax_kind


async def recognize_unconsumed_advance_cover_for_close(
    conn,
    tenant_id: UUID,
    table_session_id: UUID,
    amount_cop: Decimal,
    order_ids: List[UUID],
    tax_config: Dict[str, Any],
    entry_date: date,
    created_by: Optional[UUID],
) -> Decimal:
    cover_to_apply = Decimal(str(amount_cop or 0)).quantize(Decimal("0.01"))
    if cover_to_apply <= 0:
        return Decimal("0")

    existing = await conn.fetchval(
        """
        SELECT id
        FROM tenant_journal_entries
        WHERE tenant_id = $1
          AND source_module = $2
          AND source_id = $3
          AND status = 'posted'
        """,
        tenant_id,
        ADVANCE_COVER_SOURCE,
        table_session_id,
    )
    if existing:
        logger.info("[table advance GL] Cover for session %s already posted", table_session_id)
        return Decimal("0")

    liability_acct = await _resolve_liability_account(conn, tenant_id)
    revenue_acct = await resolve_account(
        conn, tenant_id, AccountRole.SALES_REVENUE, source="table_advance_cover"
    )
    _, _, preview_tax_amount, tax_kind = _standard_cover_gl_amounts(
        cover_to_apply, tax_config
    )
    tax_acct = None
    if tax_kind and preview_tax_amount > 0:
        tax_acct = await resolve_tax_account(
            conn, tenant_id, tax_config, tax_kind, required=True
        )

    applied = await apply_session_advances_for_close(
        conn,
        tenant_id,
        table_session_id,
        cover_to_apply,
        order_ids,
    )
    if applied <= 0:
        return Decimal("0")

    settlement, net_revenue, tax_amount, _ = _standard_cover_gl_amounts(applied, tax_config)

    async with conn.transaction():
        entry_row = await conn.fetchrow(
            """
            INSERT INTO tenant_journal_entries
                (tenant_id, entry_date, period_year, period_month,
                 description, reference, source_module, source_id, status,
                 total_debit, total_credit, created_by_user_id, posted_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'posted',
                    $9, $9, $10::uuid, NOW())
            RETURNING id
            """,
            tenant_id,
            entry_date,
            entry_date.year,
            entry_date.month,
            "Sobrante consumo mínimo mesa",
            f"table-session-cover-{table_session_id}",
            ADVANCE_COVER_SOURCE,
            table_session_id,
            float(settlement),
            str(created_by) if created_by else None,
        )
        entry_id = entry_row["id"]
        await conn.execute(
            """
            INSERT INTO tenant_journal_lines
                (journal_entry_id, account_id, debit, credit, description, line_order)
            VALUES ($1, $2, $3, 0, $4, 0)
            """,
            entry_id,
            liability_acct.id,
            float(settlement),
            f"Dr {liability_acct.code} - aplicación sobrante consumo mínimo",
        )
        await conn.execute(
            """
            INSERT INTO tenant_journal_lines
                (journal_entry_id, account_id, debit, credit, description, line_order)
            VALUES ($1, $2, 0, $3, $4, 1)
            """,
            entry_id,
            revenue_acct.id,
            float(net_revenue),
            f"Cr {revenue_acct.code} - ingreso cover consumo mínimo",
        )
        if tax_amount > 0 and tax_acct:
            await conn.execute(
                """
                INSERT INTO tenant_journal_lines
                    (journal_entry_id, account_id, debit, credit, description, line_order)
                VALUES ($1, $2, 0, $3, $4, 2)
                """,
                entry_id,
                tax_acct.id,
                float(tax_amount),
                "Cr impuesto - cover consumo mínimo",
            )

    logger.info(
        "[table advance GL] Posted cover for session %s amount=%s tax=%s",
        table_session_id,
        float(settlement),
        float(tax_amount),
    )
    return applied


async def fetch_table_session_advance_totals_for_cierre(
    conn,
    tenant_id: UUID,
    period_start: date,
    period_end: date,
    period_start_time=None,
    period_end_time=None,
) -> Dict[str, Dict[str, float]]:
    if period_start_time and period_end_time:
        receive_filter = "created_at >= $2 AND created_at < $3"
        void_filter = "voided_at >= $2 AND voided_at < $3"
        params = (tenant_id, period_start_time, period_end_time)
    else:
        receive_filter = "created_at::date >= $2 AND created_at::date <= $3"
        void_filter = "voided_at::date >= $2 AND voided_at::date <= $3"
        params = (tenant_id, period_start, period_end)

    collection_rows = await conn.fetch(
        f"""
        SELECT payment_method AS method, COALESCE(SUM(amount_cop), 0) AS total
        FROM table_session_advances
        WHERE tenant_id = $1
          AND payment_method IS NOT NULL
          AND {receive_filter}
        GROUP BY payment_method

        UNION ALL

        SELECT payment_method AS method, -COALESCE(SUM(amount_cop), 0) AS total
        FROM table_session_advances
        WHERE tenant_id = $1
          AND payment_method IS NOT NULL
          AND status = 'voided'
          AND voided_at IS NOT NULL
          AND {void_filter}
        GROUP BY payment_method
        """,
        *params,
    )

    application_rows = await conn.fetch(
        f"""
        SELECT
            COALESCE(pmg.slug, first_order.payment_method, $4) AS method,
            COALESCE(SUM(tsa.applied_amount_cop), 0) AS total
        FROM table_session_advances tsa
        LEFT JOIN LATERAL (
            SELECT o.payment_method, o.payment_method_id
            FROM orders o
            WHERE o.id = ANY(tsa.applied_order_ids)
            ORDER BY o.created_at, o.id
            LIMIT 1
        ) first_order ON true
        LEFT JOIN payment_methods pm ON pm.id = first_order.payment_method_id
        LEFT JOIN payment_method_groups pmg ON pmg.id = pm.group_id
        WHERE tsa.tenant_id = $1
          AND tsa.status = 'active'
          AND COALESCE(tsa.applied_amount_cop, 0) > 0
          AND tsa.applied_at IS NOT NULL
          AND {receive_filter.replace("created_at", "tsa.applied_at")}
        GROUP BY COALESCE(pmg.slug, first_order.payment_method, $4)
        """,
        *params,
        TABLE_SESSION_ADVANCE_PAYMENT_SLUG,
    )

    if period_start_time and period_end_time:
        cover_rows = await conn.fetch(
            """
            SELECT COALESCE(SUM(total_debit), 0) AS total
            FROM tenant_journal_entries
            WHERE tenant_id = $1
              AND source_module = $4
              AND status = 'posted'
              AND entry_date >= $2::date
              AND entry_date <= $3::date
            """,
            tenant_id,
            period_start_time,
            period_end_time,
            ADVANCE_COVER_SOURCE,
        )
    else:
        cover_rows = await conn.fetch(
            """
            SELECT COALESCE(SUM(total_debit), 0) AS total
            FROM tenant_journal_entries
            WHERE tenant_id = $1
              AND source_module = $4
              AND status = 'posted'
              AND entry_date >= $2
              AND entry_date <= $3
            """,
            tenant_id,
            period_start,
            period_end,
            ADVANCE_COVER_SOURCE,
        )

    out = {"collections": {}, "applications": {}, "cover": {"total": 0.0}}
    for row in collection_rows:
        method = row["method"]
        if method:
            out["collections"][method] = out["collections"].get(method, 0.0) + float(row["total"])
    for row in application_rows:
        method = row["method"]
        if method:
            out["applications"][method] = out["applications"].get(method, 0.0) + float(row["total"])
    for row in cover_rows:
        out["cover"]["total"] += float(row["total"] or 0)
    return out


async def _post_receive_gl(
    conn,
    tenant_id: UUID,
    advance_id: UUID,
    amount: Decimal,
    payment_method: str,
    payment_method_id: Optional[UUID],
    entry_date: date,
    created_by: Optional[UUID],
) -> Optional[UUID]:
    debit_acct = await _resolve_payment_debit_account(
        conn, tenant_id, payment_method, payment_method_id
    )
    credit_acct = await _resolve_liability_account(conn, tenant_id)
    return await _post_two_line_gl(
        conn,
        tenant_id,
        entry_date,
        "Anticipo consumo mínimo mesa",
        f"table-session-advance-{advance_id}",
        ADVANCE_RECEIVE_SOURCE,
        advance_id,
        debit_acct.id,
        credit_acct.id,
        amount,
        f"Dr {debit_acct.code} — anticipo mesa",
        f"Cr {credit_acct.code} — anticipos recibidos",
        created_by,
    )


async def _post_void_gl(
    conn,
    tenant_id: UUID,
    advance_id: UUID,
    amount: Decimal,
    payment_method: str,
    payment_method_id: Optional[UUID],
    entry_date: date,
    created_by: Optional[UUID],
) -> Optional[UUID]:
    debit_acct = await _resolve_liability_account(conn, tenant_id)
    credit_acct = await _resolve_payment_debit_account(
        conn, tenant_id, payment_method, payment_method_id
    )
    return await _post_two_line_gl(
        conn,
        tenant_id,
        entry_date,
        "Anulación anticipo consumo mínimo mesa",
        f"table-session-advance-void-{advance_id}",
        ADVANCE_VOID_SOURCE,
        advance_id,
        debit_acct.id,
        credit_acct.id,
        amount,
        f"Dr {debit_acct.code} — reverso anticipo mesa",
        f"Cr {credit_acct.code} — devolución anticipo mesa",
        created_by,
    )


async def _try_post_advance_gl(conn, post_fn, advance_id: UUID, column: str) -> Optional[UUID]:
    try:
        async with conn.transaction():
            journal_id = await post_fn()
            if journal_id:
                await conn.execute(
                    f"""
                    UPDATE table_session_advances
                    SET {column} = $1,
                        updated_at = now()
                    WHERE id = $2
                    """,
                    journal_id,
                    advance_id,
                )
            return journal_id
    except Exception as exc:
        logger.error("Table session advance GL failed: %s", exc)
        return None


async def create_session_advance(
    request: Request,
    table_id: UUID,
    amount_cop: Decimal | float,
    payment_method: str,
    payment_method_id: Optional[UUID] = None,
    notes: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    user_id = session.user_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")
    tenant_uuid = UUID(str(tenant_id))
    user_uuid = UUID(str(user_id)) if user_id else None
    amount = _amount_decimal(amount_cop)
    _validate_advance_tender(payment_method)

    async with get_db_connection() as conn:
        async with conn.transaction():
            session_row = await _get_open_session(conn, tenant_uuid, table_id, lock=True)
            await _assert_payment_selection(conn, tenant_uuid, payment_method, payment_method_id)
            if idempotency_key:
                existing = await conn.fetchrow(
                    """
                    SELECT *
                    FROM table_session_advances
                    WHERE tenant_id = $1
                      AND idempotency_key = $2
                    """,
                    tenant_uuid,
                    idempotency_key,
                )
                if existing:
                    payload = await get_session_advances_payload(
                        conn,
                        tenant_uuid,
                        existing["table_session_id"],
                    )
                    return {
                        "success": True,
                        "data": {
                            "advance": _serialize_advance(existing),
                            **payload,
                            "idempotent": True,
                        },
                    }

            advance_row = await conn.fetchrow(
                """
                INSERT INTO table_session_advances (
                    tenant_id,
                    table_session_id,
                    amount_cop,
                    payment_method,
                    payment_method_id,
                    notes,
                    created_by_user_id,
                    idempotency_key
                )
                VALUES ($1, $2, $3, $4, $5::uuid, $6, $7::uuid, $8)
                RETURNING *
                """,
                tenant_uuid,
                session_row["id"],
                amount,
                payment_method,
                str(payment_method_id) if payment_method_id else None,
                notes,
                str(user_uuid) if user_uuid else None,
                idempotency_key,
            )
            journal_id = await _try_post_advance_gl(
                conn,
                lambda: _post_receive_gl(
                    conn,
                    tenant_uuid,
                    advance_row["id"],
                    amount,
                    payment_method,
                    payment_method_id,
                    date.today(),
                    user_uuid,
                ),
                advance_row["id"],
                "journal_entry_id",
            )
            if journal_id:
                advance_row = dict(advance_row)
                advance_row["journal_entry_id"] = journal_id
            payload = await get_session_advances_payload(conn, tenant_uuid, session_row["id"])
            return {
                "success": True,
                "data": {
                    "advance": _serialize_advance(advance_row),
                    **payload,
                },
            }


async def list_session_advances(request: Request, table_id: UUID) -> Dict[str, Any]:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")
    tenant_uuid = UUID(str(tenant_id))

    async with get_db_connection(use_transaction=False) as conn:
        table_session = await _get_open_session(conn, tenant_uuid, table_id)
        payload = await get_session_advances_payload(conn, tenant_uuid, table_session["id"])
        return {
            "success": True,
            "data": {
                "table_session_id": str(table_session["id"]),
                **payload,
            },
        }


async def void_session_advance(
    request: Request,
    table_id: UUID,
    advance_id: UUID,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    user_id = session.user_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")
    tenant_uuid = UUID(str(tenant_id))
    user_uuid = UUID(str(user_id)) if user_id else None

    async with get_db_connection() as conn:
        async with conn.transaction():
            session_row = await _get_open_session(conn, tenant_uuid, table_id, lock=True)
            advance_row = await conn.fetchrow(
                """
                SELECT *
                FROM table_session_advances
                WHERE id = $1
                  AND tenant_id = $2
                  AND table_session_id = $3
                FOR UPDATE
                """,
                advance_id,
                tenant_uuid,
                session_row["id"],
            )
            if not advance_row:
                raise NotFoundError("Anticipo no encontrado")
            if advance_row["status"] == "voided":
                payload = await get_session_advances_payload(conn, tenant_uuid, session_row["id"])
                return {
                    "success": True,
                    "data": {
                        "advance": _serialize_advance(advance_row),
                        **payload,
                        "idempotent": True,
                    },
                }

            updated_row = await conn.fetchrow(
                """
                UPDATE table_session_advances
                SET status = 'voided',
                    voided_at = now(),
                    voided_by_user_id = $4::uuid,
                    void_reason = $5,
                    updated_at = now()
                WHERE id = $1
                  AND tenant_id = $2
                  AND table_session_id = $3
                  AND status = 'active'
                RETURNING *
                """,
                advance_id,
                tenant_uuid,
                session_row["id"],
                str(user_uuid) if user_uuid else None,
                reason,
            )
            journal_id = await _try_post_advance_gl(
                conn,
                lambda: _post_void_gl(
                    conn,
                    tenant_uuid,
                    advance_id,
                    Decimal(str(updated_row["amount_cop"])),
                    updated_row["payment_method"],
                    updated_row["payment_method_id"],
                    date.today(),
                    user_uuid,
                ),
                advance_id,
                "void_journal_entry_id",
            )
            if journal_id:
                updated_row = dict(updated_row)
                updated_row["void_journal_entry_id"] = journal_id
            payload = await get_session_advances_payload(conn, tenant_uuid, session_row["id"])
            return {
                "success": True,
                "data": {
                    "advance": _serialize_advance(updated_row),
                    **payload,
                },
            }

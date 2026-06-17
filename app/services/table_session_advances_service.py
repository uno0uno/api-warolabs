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
    DEFAULT_LIABILITY_CODE,
    WALLET_PAYMENT_SLUG,
    _post_two_line_gl,
    _resolve_account_id,
    _resolve_payment_debit_code,
)

logger = logging.getLogger(__name__)

ALLOWED_ADVANCE_TENDERS = {"cash", "card", "digital"}
ADVANCE_RECEIVE_SOURCE = "table_session_advance_receive"
ADVANCE_VOID_SOURCE = "table_session_advance_void"


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
    return {
        "id": str(row["id"]),
        "table_session_id": str(row["table_session_id"]),
        "amount_cop": float(row["amount_cop"] or 0),
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
        "created_at": row["created_at"].isoformat() if _row_get(row, "created_at") else None,
    }


def _advance_totals(rows: List[Any]) -> Dict[str, float]:
    active = sum(
        float(row["amount_cop"] or 0)
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
    debit_code = await _resolve_payment_debit_code(conn, payment_method, payment_method_id)
    debit_acct = await _resolve_account_id(conn, tenant_id, debit_code)
    credit_acct = await _resolve_account_id(conn, tenant_id, DEFAULT_LIABILITY_CODE)
    if not debit_acct or not credit_acct:
        logger.warning(
            "[table advance GL] Missing accounts debit=%s credit=%s tenant=%s",
            debit_code,
            DEFAULT_LIABILITY_CODE,
            tenant_id,
        )
        return None
    return await _post_two_line_gl(
        conn,
        tenant_id,
        entry_date,
        "Anticipo consumo mínimo mesa",
        f"table-session-advance-{advance_id}",
        ADVANCE_RECEIVE_SOURCE,
        advance_id,
        debit_acct,
        credit_acct,
        amount,
        f"Dr {debit_code} — anticipo mesa",
        f"Cr {DEFAULT_LIABILITY_CODE} — anticipos recibidos",
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
    credit_code = await _resolve_payment_debit_code(conn, payment_method, payment_method_id)
    debit_acct = await _resolve_account_id(conn, tenant_id, DEFAULT_LIABILITY_CODE)
    credit_acct = await _resolve_account_id(conn, tenant_id, credit_code)
    if not debit_acct or not credit_acct:
        logger.warning(
            "[table advance GL] Missing void accounts debit=%s credit=%s tenant=%s",
            DEFAULT_LIABILITY_CODE,
            credit_code,
            tenant_id,
        )
        return None
    return await _post_two_line_gl(
        conn,
        tenant_id,
        entry_date,
        "Anulación anticipo consumo mínimo mesa",
        f"table-session-advance-void-{advance_id}",
        ADVANCE_VOID_SOURCE,
        advance_id,
        debit_acct,
        credit_acct,
        amount,
        f"Dr {DEFAULT_LIABILITY_CODE} — reverso anticipo mesa",
        f"Cr {credit_code} — devolución anticipo mesa",
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

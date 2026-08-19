"""
Customer COP prepayment wallet — ledger, staff APIs, and checkout settlement.

Issue: https://github.com/uno0uno/api-warolabs/issues/369
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import Request

from app.core.exceptions import APIError, AuthenticationError
from app.core.middleware import require_valid_session
from app.database import get_db_connection
from app.services.account_role_service import (
    AccountRole,
    resolve_account,
    resolve_payment_account,
)
from app.services.customer_relationship_service import is_tenant_customer

logger = logging.getLogger(__name__)

WALLET_PAYMENT_SLUG = "customer_wallet"
ANONYMOUS_PHONE = "0000000000"
RECENT_MOVEMENTS_DEFAULT = 20


async def _resolve_liability_account(conn, tenant_id: UUID):
    return await resolve_account(
        conn, tenant_id, AccountRole.CUSTOMER_ADVANCES, source="customer_wallet"
    )


async def _resolve_payment_debit_account(
    conn,
    tenant_id: UUID,
    payment_method: str,
    payment_method_id: Optional[UUID],
):
    return await resolve_payment_account(
        conn,
        tenant_id,
        payment_method,
        payment_method_id=payment_method_id,
        source="customer_wallet",
    )


async def assert_wallet_customer_identified(conn, profile_id: UUID) -> None:
    """Wallet tender is only valid for an identified, non-anonymous customer."""

    row = await conn.fetchrow(
        "SELECT phone_number FROM profile WHERE id = $1",
        profile_id,
    )
    if not row:
        raise APIError("Cliente no encontrado", status_code=404)
    if row["phone_number"] == ANONYMOUS_PHONE:
        raise APIError(
            "La billetera requiere un cliente identificado (no anónimo)",
            status_code=400,
        )


async def _assert_tenant_customer(conn, profile_id: UUID, tenant_id: UUID) -> None:
    if not await is_tenant_customer(conn, profile_id, tenant_id):
        raise APIError("Cliente no pertenece a este negocio", status_code=404)


def validate_wallet_payment_tender(
    payment_method: str,
    cash_received: Optional[float] = None,
) -> None:
    """Reject cash-change fields for wallet payments."""

    if payment_method != WALLET_PAYMENT_SLUG:
        return
    if cash_received is not None:
        raise APIError(
            "cash_received no aplica al método billetera del cliente",
            status_code=400,
        )


async def _lock_balance_cop(conn, profile_id: UUID, tenant_id: UUID) -> Decimal:
    row = await conn.fetchrow(
        """
        SELECT balance_cop
        FROM customer_wallet_balances
        WHERE profile_id = $1 AND tenant_id = $2
        FOR UPDATE
        """,
        profile_id,
        tenant_id,
    )
    return Decimal(str(row["balance_cop"])) if row else Decimal("0")


async def _upsert_balance(
    conn,
    profile_id: UUID,
    tenant_id: UUID,
    new_balance: Decimal,
) -> None:
    await conn.execute(
        """
        INSERT INTO customer_wallet_balances (profile_id, tenant_id, balance_cop, updated_at)
        VALUES ($1, $2, $3, now())
        ON CONFLICT (profile_id, tenant_id) DO UPDATE SET
            balance_cop = EXCLUDED.balance_cop,
            updated_at = now()
        """,
        profile_id,
        tenant_id,
        new_balance,
    )


async def _insert_movement(
    conn,
    *,
    profile_id: UUID,
    tenant_id: UUID,
    movement_type: str,
    amount_cop: Decimal,
    balance_after_cop: Decimal,
    payment_method: Optional[str] = None,
    payment_method_id: Optional[UUID] = None,
    order_id: Optional[UUID] = None,
    order_payment_id: Optional[UUID] = None,
    journal_entry_id: Optional[UUID] = None,
    notes: Optional[str] = None,
    created_by_user_id: Optional[UUID] = None,
    idempotency_key: Optional[str] = None,
) -> UUID:
    row = await conn.fetchrow(
        """
        INSERT INTO customer_wallet_movements (
            profile_id, tenant_id, movement_type, amount_cop, balance_after_cop,
            payment_method, payment_method_id, order_id, order_payment_id,
            journal_entry_id, notes, created_by_user_id, idempotency_key
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
        RETURNING id
        """,
        profile_id,
        tenant_id,
        movement_type,
        amount_cop,
        balance_after_cop,
        payment_method,
        payment_method_id,
        order_id,
        order_payment_id,
        journal_entry_id,
        notes,
        created_by_user_id,
        idempotency_key,
    )
    return row["id"]


async def _post_two_line_gl(
    conn,
    tenant_id: UUID,
    entry_date: date,
    description: str,
    reference: str,
    source_module: str,
    source_id: UUID,
    debit_account_id: UUID,
    credit_account_id: UUID,
    amount: Decimal,
    debit_desc: str,
    credit_desc: str,
    created_by: Optional[UUID],
) -> Optional[UUID]:
    if amount <= 0:
        return None
    period_year = entry_date.year
    period_month = entry_date.month
    amt = float(amount)
    entry_row = await conn.fetchrow(
        """
        INSERT INTO tenant_journal_entries
            (tenant_id, entry_date, period_year, period_month,
             description, reference, source_module, source_id,
             status, total_debit, total_credit, created_by, posted_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'posted', $9, $9, $10, now())
        RETURNING id
        """,
        tenant_id,
        entry_date,
        period_year,
        period_month,
        description,
        reference,
        source_module,
        source_id,
        amt,
        created_by,
    )
    entry_id = entry_row["id"]
    await conn.execute(
        """
        INSERT INTO tenant_journal_lines
            (journal_entry_id, account_id, debit, credit, description, line_order)
        VALUES ($1, $2, $3, 0, $4, 0)
        """,
        entry_id,
        debit_account_id,
        amt,
        debit_desc,
    )
    await conn.execute(
        """
        INSERT INTO tenant_journal_lines
            (journal_entry_id, account_id, debit, credit, description, line_order)
        VALUES ($1, $2, 0, $3, $4, 1)
        """,
        entry_id,
        credit_account_id,
        amt,
        credit_desc,
    )
    return entry_id


async def _try_post_wallet_gl(
    conn,
    post_fn,
    *,
    movement_id: UUID,
) -> Optional[UUID]:
    """
    Post wallet GL inside a savepoint so a constraint/GL failure does not
    abort the outer wallet movement transaction.
    """
    try:
        async with conn.transaction():
            journal_id = await post_fn()
            if journal_id:
                await conn.execute(
                    """
                    UPDATE customer_wallet_movements
                    SET journal_entry_id = $1
                    WHERE id = $2
                    """,
                    journal_id,
                    movement_id,
                )
            return journal_id
    except Exception as exc:
        logger.error("Wallet GL failed: %s", exc)
        return None


async def _post_recharge_gl(
    conn,
    tenant_id: UUID,
    movement_id: UUID,
    amount: Decimal,
    payment_method: str,
    payment_method_id: Optional[UUID],
    entry_date: date,
    created_by: Optional[UUID],
) -> Optional[UUID]:
    credit_acct = await _resolve_liability_account(conn, tenant_id)
    debit_acct = await _resolve_payment_debit_account(
        conn, tenant_id, payment_method, payment_method_id
    )
    return await _post_two_line_gl(
        conn,
        tenant_id,
        entry_date,
        "Recarga billetera cliente",
        f"wallet-recharge-{movement_id}",
        "customer_wallet_recharge",
        movement_id,
        debit_acct.id,
        credit_acct.id,
        amount,
        f"Dr {debit_acct.code} — recarga billetera",
        f"Cr {credit_acct.code} — anticipo cliente",
        created_by,
    )


async def _post_refund_gl(
    conn,
    tenant_id: UUID,
    movement_id: UUID,
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
        "Devolución billetera cliente",
        f"wallet-refund-{movement_id}",
        "customer_wallet_refund",
        movement_id,
        debit_acct.id,
        credit_acct.id,
        amount,
        f"Dr {debit_acct.code} — devolución anticipo",
        f"Cr {credit_acct.code} — salida de caja/banco",
        created_by,
    )


async def apply_wallet_for_order(
    conn,
    profile_id: UUID,
    tenant_id: UUID,
    amount_cop: Decimal,
    order_id: UUID,
    created_by_user_id: Optional[UUID],
    order_payment_id: Optional[UUID] = None,
) -> UUID:
    """Debit wallet within the checkout transaction.

    Wallet is a payment tender, not a discount. The balance lock and
    insufficient-balance error stay backend-authoritative for both single and
    split payments.
    """
    if amount_cop <= 0:
        raise APIError("Monto de billetera inválido", status_code=400)
    await assert_wallet_customer_identified(conn, profile_id)
    current = await _lock_balance_cop(conn, profile_id, tenant_id)
    if current < amount_cop:
        raise APIError(
            f"Saldo de billetera insuficiente. Disponible: {current}, requerido: {amount_cop}",
            status_code=400,
        )
    new_balance = current - amount_cop
    movement_id = await _insert_movement(
        conn,
        profile_id=profile_id,
        tenant_id=tenant_id,
        movement_type="apply",
        amount_cop=-amount_cop,
        balance_after_cop=new_balance,
        order_id=order_id,
        order_payment_id=order_payment_id,
        created_by_user_id=created_by_user_id,
    )
    await _upsert_balance(conn, profile_id, tenant_id, new_balance)
    return movement_id


async def restore_wallet_for_order_payment_void(
    conn,
    profile_id: UUID,
    tenant_id: UUID,
    amount_cop: Decimal,
    order_id: UUID,
    order_payment_id: Optional[UUID],
    created_by_user_id: Optional[UUID],
    notes: Optional[str] = None,
) -> UUID:
    """Restore wallet balance when a wallet tender row is voided."""
    if amount_cop <= 0:
        raise APIError("Monto de reversión de billetera inválido", status_code=400)
    await assert_wallet_customer_identified(conn, profile_id)
    current = await _lock_balance_cop(conn, profile_id, tenant_id)
    new_balance = current + amount_cop
    movement_id = await _insert_movement(
        conn,
        profile_id=profile_id,
        tenant_id=tenant_id,
        movement_type="void_apply",
        amount_cop=amount_cop,
        balance_after_cop=new_balance,
        order_id=order_id,
        order_payment_id=order_payment_id,
        notes=notes,
        created_by_user_id=created_by_user_id,
    )
    await _upsert_balance(conn, profile_id, tenant_id, new_balance)
    return movement_id


async def restore_wallet_for_cancelled_order(
    conn,
    tenant_id: UUID,
    order_id: UUID,
    created_by_user_id: Optional[UUID],
    notes: Optional[str] = None,
) -> List[UUID]:
    """Restore remaining wallet apply for a cancelled sale (idempotent)."""
    rows = await conn.fetch(
        """
        SELECT
            profile_id,
            (
              COALESCE(SUM(CASE WHEN movement_type = 'apply' THEN -amount_cop ELSE 0 END), 0)
              - COALESCE(SUM(CASE WHEN movement_type = 'void_apply' THEN amount_cop ELSE 0 END), 0)
            ) AS net_applied,
            (ARRAY_AGG(order_payment_id) FILTER (WHERE order_payment_id IS NOT NULL))[1]
              AS order_payment_id
        FROM customer_wallet_movements
        WHERE tenant_id = $1 AND order_id = $2
        GROUP BY profile_id
        """,
        tenant_id,
        order_id,
    )
    if not isinstance(rows, (list, tuple)):
        return []
    restored: List[UUID] = []
    for row in rows:
        try:
            net_applied = Decimal(str(row["net_applied"] or 0))
            profile_id = row["profile_id"]
            order_payment_id = row["order_payment_id"]
        except (KeyError, TypeError):
            continue
        if not profile_id or net_applied <= 0:
            continue
        movement_id = await restore_wallet_for_order_payment_void(
            conn,
            profile_id,
            tenant_id,
            net_applied,
            order_id,
            order_payment_id,
            created_by_user_id,
            notes=notes,
        )
        restored.append(movement_id)
    return restored


async def apply_wallet_for_session_orders(
    conn,
    profile_id: UUID,
    tenant_id: UUID,
    order_rows: List[Any],
    extra_tip_cop: Decimal,
    created_by_user_id: Optional[UUID],
) -> None:
    """Debit wallet for each completed mesa order plus optional tip lump."""
    for row in order_rows:
        amt = Decimal(str(row["total_amount"]))
        if amt > 0:
            await apply_wallet_for_order(
                conn,
                profile_id,
                tenant_id,
                amt,
                row["id"],
                created_by_user_id,
            )
    if extra_tip_cop > 0:
        first_id = order_rows[0]["id"] if order_rows else None
        if first_id:
            await apply_wallet_for_order(
                conn,
                profile_id,
                tenant_id,
                extra_tip_cop,
                first_id,
                created_by_user_id,
            )


async def get_customer_wallet(
    request: Request,
    customer_id: UUID,
    limit: int = RECENT_MOVEMENTS_DEFAULT,
) -> Dict[str, Any]:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")
    limit = max(1, min(limit, 50))

    async with get_db_connection(use_transaction=False) as conn:
        await _assert_tenant_customer(conn, customer_id, UUID(str(tenant_id)))
        balance_row = await conn.fetchrow(
            """
            SELECT balance_cop, updated_at
            FROM customer_wallet_balances
            WHERE profile_id = $1 AND tenant_id = $2
            """,
            customer_id,
            tenant_id,
        )
        balance = float(balance_row["balance_cop"]) if balance_row else 0.0
        updated_at = balance_row["updated_at"] if balance_row else None
        movements = await conn.fetch(
            """
            SELECT id, movement_type, amount_cop, balance_after_cop,
                   payment_method, order_id, notes, created_at
            FROM customer_wallet_movements
            WHERE profile_id = $1 AND tenant_id = $2
            ORDER BY created_at DESC
            LIMIT $3
            """,
            customer_id,
            tenant_id,
            limit,
        )

    return {
        "success": True,
        "data": {
            "customer_id": str(customer_id),
            "balance_cop": balance,
            "updated_at": updated_at.isoformat() if updated_at else None,
            "movements": [
                {
                    "id": str(m["id"]),
                    "movement_type": m["movement_type"],
                    "amount_cop": float(m["amount_cop"]),
                    "balance_after_cop": float(m["balance_after_cop"]),
                    "payment_method": m["payment_method"],
                    "order_id": str(m["order_id"]) if m["order_id"] else None,
                    "notes": m["notes"],
                    "created_at": m["created_at"].isoformat(),
                }
                for m in movements
            ],
        },
    }


async def recharge_customer_wallet(
    request: Request,
    customer_id: UUID,
    amount_cop: Decimal,
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
    if amount_cop <= 0:
        raise APIError("El monto de recarga debe ser mayor a cero", status_code=400)
    if payment_method == WALLET_PAYMENT_SLUG:
        raise APIError(
            "Use cash, card o digital como método de recarga",
            status_code=400,
        )
    validate_wallet_payment_tender(payment_method)

    async with get_db_connection() as conn:
        async with conn.transaction():
            await _assert_tenant_customer(conn, customer_id, UUID(str(tenant_id)))
            await assert_wallet_customer_identified(conn, customer_id)
            if idempotency_key:
                existing = await conn.fetchval(
                    """
                    SELECT id FROM customer_wallet_movements
                    WHERE tenant_id = $1 AND idempotency_key = $2
                    """,
                    tenant_id,
                    idempotency_key,
                )
                if existing:
                    bal = await conn.fetchval(
                        """
                        SELECT balance_cop FROM customer_wallet_balances
                        WHERE profile_id = $1 AND tenant_id = $2
                        """,
                        customer_id,
                        tenant_id,
                    )
                    return {
                        "success": True,
                        "data": {
                            "movement_id": str(existing),
                            "balance_cop": float(bal or 0),
                            "idempotent": True,
                        },
                    }

            current = await _lock_balance_cop(conn, customer_id, UUID(str(tenant_id)))
            new_balance = current + amount_cop
            movement_id = await _insert_movement(
                conn,
                profile_id=customer_id,
                tenant_id=UUID(str(tenant_id)),
                movement_type="receive",
                amount_cop=amount_cop,
                balance_after_cop=new_balance,
                payment_method=payment_method,
                payment_method_id=payment_method_id,
                notes=notes,
                created_by_user_id=UUID(str(user_id)) if user_id else None,
                idempotency_key=idempotency_key,
            )
            await _upsert_balance(conn, customer_id, UUID(str(tenant_id)), new_balance)
            await _try_post_wallet_gl(
                conn,
                lambda: _post_recharge_gl(
                    conn,
                    UUID(str(tenant_id)),
                    movement_id,
                    amount_cop,
                    payment_method,
                    payment_method_id,
                    date.today(),
                    UUID(str(user_id)) if user_id else None,
                ),
                movement_id=movement_id,
            )

    return {
        "success": True,
        "data": {
            "movement_id": str(movement_id),
            "balance_cop": float(new_balance),
            "idempotent": False,
        },
    }


async def refund_customer_wallet(
    request: Request,
    customer_id: UUID,
    amount_cop: Decimal,
    payment_method: str,
    payment_method_id: Optional[UUID] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    user_id = session.user_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")
    if amount_cop <= 0:
        raise APIError("El monto de devolución debe ser mayor a cero", status_code=400)
    validate_wallet_payment_tender(payment_method)

    async with get_db_connection() as conn:
        async with conn.transaction():
            await _assert_tenant_customer(conn, customer_id, UUID(str(tenant_id)))
            await assert_wallet_customer_identified(conn, customer_id)
            current = await _lock_balance_cop(conn, customer_id, UUID(str(tenant_id)))
            if current < amount_cop:
                raise APIError(
                    f"Saldo insuficiente para devolución. Disponible: {current}",
                    status_code=400,
                )
            new_balance = current - amount_cop
            movement_id = await _insert_movement(
                conn,
                profile_id=customer_id,
                tenant_id=UUID(str(tenant_id)),
                movement_type="refund",
                amount_cop=-amount_cop,
                balance_after_cop=new_balance,
                payment_method=payment_method,
                payment_method_id=payment_method_id,
                notes=notes,
                created_by_user_id=UUID(str(user_id)) if user_id else None,
            )
            await _upsert_balance(conn, customer_id, UUID(str(tenant_id)), new_balance)
            await _try_post_wallet_gl(
                conn,
                lambda: _post_refund_gl(
                    conn,
                    UUID(str(tenant_id)),
                    movement_id,
                    amount_cop,
                    payment_method,
                    payment_method_id,
                    date.today(),
                    UUID(str(user_id)) if user_id else None,
                ),
                movement_id=movement_id,
            )

    return {
        "success": True,
        "data": {
            "movement_id": str(movement_id),
            "balance_cop": float(new_balance),
        },
    }


async def fetch_wallet_recharge_totals_for_cierre(
    conn,
    tenant_id: UUID,
    period_start: date,
    period_end: date,
    period_start_time: Optional[datetime] = None,
    period_end_time: Optional[datetime] = None,
) -> Dict[str, float]:
    """
    Sum wallet recharges by payment_method slug for arqueo cash/card/digital totals.
    Only movement_type='receive' counts as caja inflow.
    """
    if period_start_time and period_end_time:
        rows = await conn.fetch(
            """
            SELECT payment_method AS method, COALESCE(SUM(amount_cop), 0) AS total
            FROM customer_wallet_movements
            WHERE tenant_id = $1
              AND movement_type = 'receive'
              AND payment_method IS NOT NULL
              AND created_at >= $2 AND created_at < $3
            GROUP BY payment_method
            """,
            tenant_id,
            period_start_time,
            period_end_time,
        )
    else:
        rows = await conn.fetch(
            """
            SELECT payment_method AS method, COALESCE(SUM(amount_cop), 0) AS total
            FROM customer_wallet_movements
            WHERE tenant_id = $1
              AND movement_type = 'receive'
              AND payment_method IS NOT NULL
              AND created_at::date >= $2 AND created_at::date <= $3
            GROUP BY payment_method
            """,
            tenant_id,
            period_start,
            period_end,
        )
    out: Dict[str, float] = {}
    for row in rows:
        m = row["method"]
        if m:
            out[m] = out.get(m, 0.0) + float(row["total"])
    return out

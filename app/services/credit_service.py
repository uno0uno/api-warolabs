"""
Credit Service
Handles credit payment registration, payment history, and open-credit order listing.

Issue: https://github.com/uno0uno/warocol.com/issues/294
"""
from typing import Any, Dict, List, Optional
from uuid import UUID
from decimal import Decimal
from datetime import date, datetime, timezone
from fastapi import Request
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError, APIError
from app.services.operation_events_service import DOMAIN_FINANZAS, record_operation_event
from app.services.account_role_service import (
    AccountRole,
    resolve_account,
    resolve_payment_account,
)
import logging

logger = logging.getLogger(__name__)


async def sync_order_split_credit_status(
    conn,
    order_id: UUID,
    *,
    settlement_complete: bool = False,
) -> str:
    """Derive payment_status + credit_paid_amount from active order_payments.

    Split tenders that include ``credit`` must leave receivable outstanding so
    Cartera (payment_status IN credit/partial) can show the debt. Non-credit
    tenders (cash, wallet, card, …) seed ``credit_paid_amount``.

    When there is no credit tender, ``payment_status='paid'`` is only written if
    ``settlement_complete`` is true (avoids marking mid-split orders as paid).

    Returns the payment_status written on the order.
    """
    order_row = await conn.fetchrow(
        "SELECT total_amount, payment_status FROM orders WHERE id = $1",
        order_id,
    )
    if not order_row:
        return "paid"

    total = round(float(order_row["total_amount"] or 0), 2)
    payments = await conn.fetch(
        """
        SELECT payment_method, amount
        FROM order_payments
        WHERE order_id = $1 AND voided_at IS NULL
        """,
        order_id,
    )
    credit_sum = round(
        sum(float(p["amount"]) for p in payments if p["payment_method"] == "credit"),
        2,
    )
    non_credit_sum = round(
        sum(float(p["amount"]) for p in payments if p["payment_method"] != "credit"),
        2,
    )

    if credit_sum <= 0.01:
        if not settlement_complete:
            status = "partial"
            credit_paid = 0.0
        else:
            status = "paid"
            credit_paid = 0.0
    elif non_credit_sum <= 0.01:
        status = "credit"
        credit_paid = 0.0
    else:
        status = "partial"
        # Remaining ≈ credit tender(s), capped by order merchandise total
        # (tips may sit in order_payments above total_amount).
        credit_paid = max(0.0, round(total - min(credit_sum, total), 2))

    await conn.execute(
        """
        UPDATE orders
        SET payment_status = $2,
            credit_paid_amount = $3
        WHERE id = $1
        """,
        order_id,
        status,
        credit_paid,
    )
    return status


async def _resolve_credit_payment_debit_account(
    conn,
    tenant_id: UUID,
    payment_method: str,
    group_id: UUID,
    payment_method_id: Optional[UUID],
):
    return await resolve_payment_account(
        conn,
        tenant_id,
        payment_method,
        payment_method_id=payment_method_id,
        payment_group_id=group_id,
        source="credit_payment",
    )


async def _post_credit_payment_gl(
    conn,
    tenant_id: UUID,
    payment_id: UUID,
    order_id: UUID,
    amount: Decimal,
    payment_method: str,
    group_id: UUID,
    payment_method_id: Optional[UUID],
    payment_date_value,
    created_by: Optional[UUID],
) -> Optional[UUID]:
    existing = await conn.fetchval(
        """
        SELECT id
        FROM tenant_journal_entries
        WHERE tenant_id = $1
          AND source_module = 'cartera'
          AND source_id = $2
        """,
        tenant_id,
        payment_id,
    )
    if existing:
        logger.info("[credit GL] Payment %s already posted as journal %s", payment_id, existing)
        return existing

    debit_account = await _resolve_credit_payment_debit_account(
        conn,
        tenant_id,
        payment_method,
        group_id,
        payment_method_id,
    )
    receivable_account = await resolve_account(
        conn,
        tenant_id,
        AccountRole.ACCOUNTS_RECEIVABLE,
        source="credit_payment",
    )

    entry_date = (
        payment_date_value.date()
        if isinstance(payment_date_value, datetime)
        else payment_date_value
    )
    amt = float(amount)
    description = f"Abono cartera orden {order_id}"
    entry_row = await conn.fetchrow(
        """
        INSERT INTO tenant_journal_entries
            (tenant_id, entry_date, period_year, period_month,
             description, reference, source_module, source_id,
             status, total_debit, total_credit, created_by, posted_at)
        VALUES ($1, $2, $3, $4, $5, $6, 'cartera', $7,
                'posted', $8, $8, $9, now())
        RETURNING id
        """,
        tenant_id,
        entry_date,
        entry_date.year,
        entry_date.month,
        description,
        f"credit-payment-{payment_id}",
        payment_id,
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
        debit_account.id,
        amt,
        f"Dr {debit_account.code} - abono cartera",
    )
    await conn.execute(
        """
        INSERT INTO tenant_journal_lines
            (journal_entry_id, account_id, debit, credit, description, line_order)
        VALUES ($1, $2, 0, $3, $4, 1)
        """,
        entry_id,
        receivable_account.id,
        amt,
        f"Cr {receivable_account.code} - clientes por cobrar",
    )
    return entry_id


async def register_credit_payment(
    request: Request,
    order_id: UUID,
    amount: Decimal,
    payment_method: str,
    payment_method_id: Optional[UUID] = None,
    notes: Optional[str] = None,
    payment_date: Optional[date] = None,
) -> dict:
    """
    Register a payment against a credit order.

    1. Fetches and validates the order (must belong to tenant, status must be 'credit' or 'partial').
    2. Over-payment guard: amount must not exceed (total - credit_paid_amount).
    3. Inserts into credit_payments.
    4. Updates orders.credit_paid_amount and orders.payment_status atomically.
    5. Transition: credit -> partial (partial payment) -> paid (full payment).
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id
        user_id = session_context.user_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection() as conn:
            async with conn.transaction():
                # 1. Fetch order and verify ownership + credit state
                order_row = await conn.fetchrow(
                    """
                    SELECT id, tenant_id, customer_id, total_amount,
                           payment_status, credit_paid_amount
                    FROM orders
                    WHERE id = $1 AND tenant_id = $2
                    """,
                    order_id,
                    tenant_id,
                )

                if not order_row:
                    raise APIError("Orden no encontrada", status_code=404)

                if order_row["payment_status"] == "paid":
                    raise APIError(
                        "Esta orden ya está completamente pagada",
                        status_code=400,
                    )

                if order_row["payment_status"] not in ("credit", "partial"):
                    raise APIError(
                        "Esta orden no es una orden a crédito",
                        status_code=400,
                    )

                total = Decimal(str(order_row["total_amount"]))
                already_paid = Decimal(str(order_row["credit_paid_amount"]))
                remaining = total - already_paid

                # 2. Over-payment guard
                if amount > remaining:
                    raise APIError(
                        f"El monto ({amount}) excede el saldo pendiente ({remaining}). "
                        "No se permiten sobre-pagos.",
                        status_code=400,
                    )

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
                        f"Método de pago '{payment_method}' no es válido para este restaurante.",
                        status_code=400,
                        details={"code": "payment_method_invalid"},
                    )

                if payment_method_id:
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
                            "El método seleccionado no pertenece al grupo elegido.",
                            status_code=400,
                            details={"code": "payment_method_id_invalid"},
                        )

                # 3. Insert payment record
                effective_payment_date = (
                    datetime.combine(payment_date, datetime.min.time()).replace(tzinfo=timezone.utc)
                    if payment_date
                    else None
                )

                payment_row = await conn.fetchrow(
                    """
                    INSERT INTO credit_payments (
                        order_id, customer_id, tenant_id,
                        amount, payment_method, payment_method_id, notes,
                        created_by_user_id,
                        payment_date
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, COALESCE($9, now()))
                    RETURNING id, payment_date, created_at
                    """,
                    order_id,
                    order_row["customer_id"],
                    tenant_id,
                    amount,
                    payment_method,
                    payment_method_id,
                    notes,
                    user_id,
                    effective_payment_date,
                )

                # 4. Update denormalized counter and resolve new payment_status
                new_paid = already_paid + amount
                if new_paid >= total:
                    new_status = "paid"
                elif new_paid > 0:
                    new_status = "partial"
                else:
                    new_status = "credit"

                await conn.execute(
                    """
                    UPDATE orders
                    SET credit_paid_amount = $1,
                        payment_status = $2
                    WHERE id = $3
                    """,
                    new_paid,
                    new_status,
                    order_id,
                )

                await _post_credit_payment_gl(
                    conn,
                    tenant_id,
                    payment_row["id"],
                    order_id,
                    amount,
                    payment_method,
                    group_row["id"],
                    payment_method_id,
                    payment_row["payment_date"],
                    user_id,
                )

                logger.info(
                    f"[register_credit_payment] order={order_id} "
                    f"amount={amount} new_status={new_status} "
                    f"new_paid={new_paid}/{total}"
                )

                await record_operation_event(
                    conn,
                    tenant_id,
                    domain=DOMAIN_FINANZAS,
                    channel=None,
                    action="credit_payment_registered",
                    actor_user_id=user_id,
                    order_id=order_id,
                    payload={
                        "entity_type": "credit_payment",
                        "entity_id": str(payment_row["id"]),
                        "amount": float(amount),
                        "new_payment_status": new_status,
                    },
                )

                return {
                    "success": True,
                    "message": "Pago registrado exitosamente",
                    "data": {
                        "payment_id": str(payment_row["id"]),
                        "order_id": str(order_id),
                        "amount": float(amount),
                        "payment_method": payment_method,
                        "payment_method_id": str(payment_method_id) if payment_method_id else None,
                        "payment_date": payment_row["payment_date"].isoformat(),
                        "new_payment_status": new_status,
                        "credit_paid_amount": float(new_paid),
                        "remaining_amount": float(total - new_paid),
                    },
                }

    except (AuthenticationError, APIError):
        raise
    except Exception as exc:
        logger.error(f"Error registering credit payment: {exc}")
        raise APIError(f"Error registering credit payment: {exc}", status_code=500)


async def get_credit_payments(request: Request, order_id: UUID) -> dict:
    """
    List all payment records for a credit order (must belong to the current tenant).
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection(use_transaction=False) as conn:
            # Verify order belongs to tenant
            order_row = await conn.fetchrow(
                "SELECT id, total_amount, payment_status, credit_paid_amount "
                "FROM orders WHERE id = $1 AND tenant_id = $2",
                order_id,
                tenant_id,
            )
            if not order_row:
                raise APIError("Orden no encontrada", status_code=404)

            rows = await conn.fetch(
                """
                SELECT
                    cp.id,
                    cp.amount,
                    cp.payment_method,
                    cp.payment_method_id,
                    cp.payment_date,
                    cp.notes,
                    cp.created_at,
                    cp.created_by_user_id
                FROM credit_payments cp
                WHERE cp.order_id = $1 AND cp.tenant_id = $2
                ORDER BY cp.payment_date ASC
                """,
                order_id,
                tenant_id,
            )

        return {
            "success": True,
            "data": {
                "order_id": str(order_id),
                "total_amount": float(order_row["total_amount"]),
                "payment_status": order_row["payment_status"],
                "credit_paid_amount": float(order_row["credit_paid_amount"]),
                "remaining_amount": float(
                    Decimal(str(order_row["total_amount"]))
                    - Decimal(str(order_row["credit_paid_amount"]))
                ),
                "payments": [
                    {
                        "id": str(row["id"]),
                        "amount": float(row["amount"]),
                        "payment_method": row["payment_method"],
                        "payment_method_id": (
                            str(row["payment_method_id"])
                            if row["payment_method_id"]
                            else None
                        ),
                        "payment_date": row["payment_date"].isoformat(),
                        "notes": row["notes"],
                        "created_at": row["created_at"].isoformat(),
                    }
                    for row in rows
                ],
            },
        }

    except (AuthenticationError, APIError):
        raise
    except Exception as exc:
        logger.error(f"Error fetching credit payments: {exc}")
        raise APIError(f"Error fetching credit payments: {exc}", status_code=500)


async def list_credit_orders(
    request: Request,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """
    List all open credit orders (payment_status IN ('credit', 'partial')) for the tenant.
    Used by the Cartera view (#295).
    """
    try:
        session_context = require_valid_session(request)
        tenant_id = session_context.tenant_id

        if not tenant_id:
            raise AuthenticationError("Tenant ID is required")

        async with get_db_connection(use_transaction=False) as conn:
            rows = await conn.fetch(
                """
                SELECT
                    o.id,
                    o.order_number,
                    o.order_date,
                    o.total_amount,
                    o.payment_status,
                    o.credit_paid_amount,
                    o.credit_due_date,
                    o.payment_method,
                    p.id   AS customer_id,
                    p.name AS customer_name,
                    p.phone_number AS customer_phone,
                    COUNT(*) OVER() AS total_count
                FROM orders o
                LEFT JOIN profile p ON o.customer_id = p.id
                WHERE o.tenant_id = $1
                  AND o.payment_status IN ('credit', 'partial')
                ORDER BY o.order_date DESC
                LIMIT $2 OFFSET $3
                """,
                tenant_id,
                limit,
                offset,
            )

        total_count = rows[0]["total_count"] if rows else 0

        return {
            "success": True,
            "data": [
                {
                    "id": str(row["id"]),
                    "order_number": int(row["order_number"]),
                    "order_date": row["order_date"].isoformat(),
                    "total_amount": float(row["total_amount"]),
                    "payment_status": row["payment_status"],
                    "credit_paid_amount": float(row["credit_paid_amount"]),
                    "remaining_amount": float(
                        Decimal(str(row["total_amount"]))
                        - Decimal(str(row["credit_paid_amount"]))
                    ),
                    "credit_due_date": str(row["credit_due_date"]) if row["credit_due_date"] is not None else None,
                    "payment_method": row["payment_method"],
                    "customer": {
                        "id": str(row["customer_id"]) if row["customer_id"] else None,
                        "name": row["customer_name"],
                        "phone": row["customer_phone"],
                    },
                }
                for row in rows
            ],
            "pagination": {
                "total": total_count,
                "limit": limit,
                "offset": offset,
                "has_more": (offset + limit) < total_count,
            },
        }

    except AuthenticationError:
        raise
    except Exception as exc:
        logger.error(f"Error listing credit orders: {exc}")
        raise APIError(f"Error listing credit orders: {exc}", status_code=500)


async def fetch_credit_payment_totals_for_cierre(
    conn,
    tenant_id: UUID,
    period_start: date,
    period_end: date,
    period_start_time: Optional[datetime] = None,
    period_end_time: Optional[datetime] = None,
) -> Dict[str, float]:
    """
    Sum cartera abonos by payment_method slug for arqueo cash/card/digital totals.
    Uses payment_date (business timestamp) with shift half-open windows.
    """
    rows = await _fetch_credit_payment_cierre_rows(
        conn,
        tenant_id,
        period_start,
        period_end,
        period_start_time,
        period_end_time,
    )
    out: Dict[str, float] = {}
    for row in rows:
        slug = row["group_slug"]
        if slug:
            out[slug] = out.get(slug, 0.0) + float(row["total"])
    return out


async def fetch_credit_payment_breakdown_for_cierre(
    conn,
    tenant_id: UUID,
    period_start: date,
    period_end: date,
    period_start_time: Optional[datetime] = None,
    period_end_time: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Per-method cartera abono rows for arqueo payment breakdown."""
    return await _fetch_credit_payment_cierre_rows(
        conn,
        tenant_id,
        period_start,
        period_end,
        period_start_time,
        period_end_time,
    )


async def _fetch_credit_payment_cierre_rows(
    conn,
    tenant_id: UUID,
    period_start: date,
    period_end: date,
    period_start_time: Optional[datetime] = None,
    period_end_time: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    if period_start_time and period_end_time:
        rows = await conn.fetch(
            """
            SELECT
                cp.payment_method AS group_slug,
                COALESCE(pm.name, cp.payment_method) AS method_name,
                COALESCE(SUM(cp.amount), 0) AS total
            FROM credit_payments cp
            LEFT JOIN payment_methods pm ON pm.id = cp.payment_method_id
            WHERE cp.tenant_id = $1
              AND cp.payment_method IS NOT NULL
              AND cp.payment_date >= $2
              AND cp.payment_date < $3
            GROUP BY cp.payment_method, COALESCE(pm.name, cp.payment_method)
            """,
            tenant_id,
            period_start_time,
            period_end_time,
        )
    else:
        rows = await conn.fetch(
            """
            SELECT
                cp.payment_method AS group_slug,
                COALESCE(pm.name, cp.payment_method) AS method_name,
                COALESCE(SUM(cp.amount), 0) AS total
            FROM credit_payments cp
            LEFT JOIN payment_methods pm ON pm.id = cp.payment_method_id
            WHERE cp.tenant_id = $1
              AND cp.payment_method IS NOT NULL
              AND cp.payment_date::date >= $2
              AND cp.payment_date::date <= $3
            GROUP BY cp.payment_method, COALESCE(pm.name, cp.payment_method)
            """,
            tenant_id,
            period_start,
            period_end,
        )
    return [
        {
            "group_slug": row["group_slug"],
            "method_name": row["method_name"],
            "total": float(row["total"]),
        }
        for row in rows
    ]

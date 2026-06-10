"""
Credit Service
Handles credit payment registration, payment history, and open-credit order listing.

Issue: https://github.com/uno0uno/warocol.com/issues/294
"""
from typing import Optional
from uuid import UUID
from decimal import Decimal
from datetime import date, datetime, timezone
from fastapi import Request
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import AuthenticationError, APIError
import logging

logger = logging.getLogger(__name__)

_PAYMENT_DEBIT_FALLBACKS = {
    "cash": "1105",
    "digital": "1110",
    "card": "1110",
    "credit": "1305",
}
_CARTERA_RECEIVABLE_CODE = "1305"


async def _resolve_account_id(conn, tenant_id: UUID, code: str) -> Optional[UUID]:
    return await conn.fetchval(
        """
        SELECT id
        FROM tenant_accounts
        WHERE tenant_id = $1 AND code = $2 AND is_active = true
        """,
        tenant_id,
        code,
    )


async def _resolve_credit_payment_debit_code(
    conn,
    tenant_id: UUID,
    payment_method: str,
    group_id: UUID,
    payment_method_id: Optional[UUID],
) -> str:
    if payment_method_id:
        method_code = await conn.fetchval(
            """
            SELECT COALESCE(pm.gl_account_code, pmg.gl_account_code)
            FROM payment_methods pm
            JOIN payment_method_groups pmg ON pm.group_id = pmg.id
            WHERE pm.id = $1
              AND pm.tenant_id = $2
              AND pm.group_id = $3
            """,
            payment_method_id,
            tenant_id,
            group_id,
        )
        if method_code:
            return str(method_code)

    group_code = await conn.fetchval(
        """
        SELECT gl_account_code
        FROM payment_method_groups
        WHERE id = $1
          AND is_active = true
          AND (tenant_id IS NULL OR tenant_id = $2)
        """,
        group_id,
        tenant_id,
    )
    if group_code:
        return str(group_code)

    return _PAYMENT_DEBIT_FALLBACKS.get(payment_method or "", "1105")


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

    debit_code = await _resolve_credit_payment_debit_code(
        conn,
        tenant_id,
        payment_method,
        group_id,
        payment_method_id,
    )
    credit_code = _CARTERA_RECEIVABLE_CODE
    debit_account_id = await _resolve_account_id(conn, tenant_id, debit_code)
    credit_account_id = await _resolve_account_id(conn, tenant_id, credit_code)
    if not debit_account_id or not credit_account_id:
        logger.warning(
            "[credit GL] Missing accounts debit=%s credit=%s tenant=%s payment=%s",
            debit_code,
            credit_code,
            tenant_id,
            payment_id,
        )
        raise APIError(
            "No se pudo registrar el asiento contable del abono de cartera.",
            status_code=500,
            details={"code": "credit_payment_gl_account_missing"},
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
        debit_account_id,
        amt,
        f"Dr {debit_code} - abono cartera",
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
        f"Cr {credit_code} - clientes por cobrar",
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

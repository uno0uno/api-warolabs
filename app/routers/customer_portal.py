"""
Customer Portal Router
Authenticated endpoints for customers to manage their own data.
Authentication: waro_customer_session JWT cookie (set by POST /online/otp/verify)
"""
from fastapi import APIRouter, Depends, Request, Response, Query
from typing import Optional
from uuid import UUID
import json
from datetime import datetime, timedelta
from app.dependencies.customer_auth import get_current_customer
from app.core.security import clear_customer_cookie
from app.core.middleware import get_tenant_context
from app.database import get_db_connection
from app.services.waros_service import _eval_rule
from app.services.customer_orders_service import cancel_customer_order, get_customer_order_detail, get_customer_orders_list

router = APIRouter(prefix="/customer", tags=["Customer Portal"])


@router.get("/me")
async def get_customer_me(current_customer: dict = Depends(get_current_customer)):
    """
    Return the authenticated customer's identity.

    Requires: waro_customer_session cookie

    Returns:
        - customer_id: UUID string
        - email: Customer email address
    """
    return {
        "customer_id": current_customer["customer_id"],
        "email": current_customer["email"],
    }


@router.get("/orders")
async def list_orders(
    current_customer: dict = Depends(get_current_customer),
    status: Optional[str] = Query(None, description="Comma-separated statuses: pending,confirmed,preparing,delivered,completed,cancelled"),
):
    """
    List all orders for the authenticated customer, sorted newest-first.

    Requires: waro_customer_session cookie

    Optional query param:
    - status: comma-separated filter e.g. ?status=pending,confirmed
    """
    return await get_customer_orders_list(
        customer_id=current_customer["customer_id"],
        status_filter=status,
    )


@router.get("/orders/{order_id}")
async def get_order_detail(
    order_id: UUID,
    current_customer: dict = Depends(get_current_customer),
):
    """
    Return full detail of a single order belonging to the authenticated customer.

    Requires: waro_customer_session cookie

    Returns 404 if order doesn't exist or doesn't belong to this customer.
    Returns 401 if no valid session cookie.
    """
    return await get_customer_order_detail(
        order_id=order_id,
        customer_id=current_customer["customer_id"],
    )


@router.post("/orders/{order_id}/cancel")
async def cancel_order(
    order_id: UUID,
    current_customer: dict = Depends(get_current_customer),
):
    """
    Cancel an order belonging to the authenticated customer.

    Requires: waro_customer_session cookie

    Returns 404 if order doesn't exist or doesn't belong to this customer.
    Returns 409 if order is not in a cancellable status (pending or confirmed).
    Returns 401 if no valid session cookie.
    """
    return await cancel_customer_order(
        order_id=order_id,
        customer_id=current_customer["customer_id"],
    )


@router.get("/waros/summary")
async def get_waros_summary(
    request: Request,
    current_customer: dict = Depends(get_current_customer),
):
    """
    Return WaRos wallet balance for the authenticated customer in the current tenant.
    Returns 0 balance if no wallet exists (never 404).
    """
    tenant_context = get_tenant_context(request)
    if not tenant_context.is_valid:
        return {"current_balance": 0, "lifetime_earned": 0, "lifetime_spent": 0}

    customer_id = UUID(current_customer["customer_id"])
    tenant_id = tenant_context.tenant_id

    async with get_db_connection(use_transaction=False) as conn:
        row = await conn.fetchrow(
            """
            SELECT current_balance, lifetime_earned, lifetime_spent
            FROM waros_wallets
            WHERE profile_id = $1 AND tenant_id = $2
            """,
            customer_id,
            tenant_id,
        )

    return {
        "current_balance": int(row["current_balance"]) if row else 0,
        "lifetime_earned": int(row["lifetime_earned"]) if row else 0,
        "lifetime_spent": int(row["lifetime_spent"]) if row else 0,
    }


@router.get("/waros/estimate")
async def estimate_waros(
    request: Request,
    total_amount: float = Query(..., gt=0),
    current_customer: dict = Depends(get_current_customer),
):
    """
    Read-only estimate of WaRos that would be earned for a cart total.
    Uses active earning rules configured for this tenant.
    """
    tenant_context = get_tenant_context(request)
    if not tenant_context.is_valid:
        return {"estimated_waros": 0, "system_enabled": False, "breakdown": []}

    customer_id = UUID(current_customer["customer_id"])
    tenant_id = tenant_context.tenant_id

    async with get_db_connection(use_transaction=False) as conn:
        config_row = await conn.fetchrow(
            "SELECT is_enabled, max_daily_waros FROM gamification_config WHERE tenant_id = $1",
            tenant_id,
        )
        if not config_row or not config_row["is_enabled"]:
            return {"estimated_waros": 0, "system_enabled": False, "breakdown": []}

        rule_rows = await conn.fetch(
            "SELECT rule_type, config FROM waro_earning_rules WHERE tenant_id = $1 AND is_active = true",
            tenant_id,
        )
        if not rule_rows:
            return {"estimated_waros": 0, "system_enabled": True, "breakdown": []}

        active_types = {r["rule_type"] for r in rule_rows}
        max_daily = int(config_row["max_daily_waros"] or 0)

        total_completed = 0
        if "purchase_count" in active_types:
            count_row = await conn.fetchrow(
                "SELECT COUNT(*) AS total FROM orders WHERE customer_id = $1 AND tenant_id = $2 AND status = 'completed'",
                customer_id, tenant_id,
            )
            total_completed = int(count_row["total"]) + 1

        freq_count = 0
        if "frequency" in active_types:
            freq_cfg = next((r["config"] for r in rule_rows if r["rule_type"] == "frequency"), {})
            if isinstance(freq_cfg, str):
                freq_cfg = json.loads(freq_cfg)
            within_days = int(freq_cfg.get("within_days", 60))
            cutoff = datetime.now() - timedelta(days=within_days)
            freq_row = await conn.fetchrow(
                "SELECT COUNT(*) AS freq_count FROM orders WHERE customer_id = $1 AND tenant_id = $2 AND status = 'completed' AND created_at >= $3",
                customer_id, tenant_id, cutoff,
            )
            freq_count = int(freq_row["freq_count"]) + 1

        today_earned = 0
        if max_daily > 0:
            today_row = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(waros_amount), 0) AS today_earned
                FROM waros_transactions
                WHERE profile_id = $1 AND tenant_id = $2
                  AND transaction_type = 'earned'
                  AND created_at >= CURRENT_DATE
                """,
                customer_id, tenant_id,
            )
            today_earned = int(today_row["today_earned"])

    breakdown = []
    total_waros = 0

    for rule in rule_rows:
        rule_type = rule["rule_type"]
        cfg = rule["config"]
        if isinstance(cfg, str):
            cfg = json.loads(cfg)

        earned = _eval_rule(
            rule_type=rule_type,
            config=cfg,
            total_amount=total_amount,
            total_completed=total_completed,
            total_qty=0,
            freq_count=freq_count,
        )
        breakdown.append({"rule_type": rule_type, "waros": earned, "is_active": True})
        total_waros += earned

    if max_daily > 0 and total_waros > 0:
        remaining = max(0, max_daily - today_earned)
        total_waros = min(total_waros, remaining)

    return {"estimated_waros": total_waros, "system_enabled": True, "breakdown": breakdown}


@router.post("/logout")
async def customer_logout(response: Response):
    """
    Log out customer by clearing the waro_customer_session cookie.
    """
    clear_customer_cookie(response)
    return {"success": True, "message": "Logged out"}

"""
Cartera Router
Endpoints for the accounts-receivable / portfolio view.

Issue: https://github.com/uno0uno/warocol.com/issues/308
"""
from fastapi import APIRouter, Request, Query
from typing import Optional
from uuid import UUID
from app.services import cartera_service

router = APIRouter(prefix="/cartera", tags=["cartera"])


@router.get("/summary")
async def cartera_summary(request: Request):
    """
    Global portfolio summary for the current tenant.

    Returns:
    - total_outstanding: sum of all unpaid credit balances
    - customer_count: distinct customers with open credit
    - overdue_count: customers with at least one overdue order
    - overdue_amount: outstanding amount on overdue orders

    Overdue = credit_due_date in the past, or no due_date set and order > 30 days old.
    """
    return await cartera_service.get_cartera_summary(request)


@router.get("/customers")
async def list_cartera_customers(
    request: Request,
    status: Optional[str] = Query("all", description="Filter: all | overdue | current"),
    sort: Optional[str] = Query(
        "balance_desc",
        description="Sort: balance_desc | name_asc | days_desc",
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """
    Paginated list of customers with outstanding credit balance.

    Excludes customers whose credit orders are all fully paid.

    Each row includes:
    - customer_id, name, phone
    - total_outstanding: sum of remaining balances
    - oldest_order_days: days since the oldest unpaid order became overdue
    - order_count: number of open credit orders
    - status: current | overdue
    """
    return await cartera_service.list_cartera_customers(
        request,
        status=status or "all",
        sort=sort or "balance_desc",
        limit=limit,
        offset=offset,
    )


@router.get("/customers/{customer_id}")
async def get_customer_cartera(
    request: Request,
    customer_id: UUID,
):
    """
    Credit detail for a single customer.

    Returns:
    - customer info (id, name, phone, email)
    - summary (total_outstanding, order_count, overdue_count, overdue_amount)
    - orders: list of open credit orders, each with:
        order_number, date, total_amount, credit_paid_amount, remaining,
        due_date, days_outstanding, payment_status, is_overdue,
        payment_history (list of credit payment records)
    """
    return await cartera_service.get_customer_cartera(request, customer_id)


@router.get("/aging")
async def cartera_aging(request: Request):
    """
    Aging bucket report — computed at query time (no caching).

    Returns 4 buckets: 0-30d / 31-60d / 61-90d / 90+d
    Each bucket: label, customer_count, total_amount
    """
    return await cartera_service.get_cartera_aging(request)

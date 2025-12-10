"""
Orders Router
Endpoints for listing and managing orders
"""
from fastapi import APIRouter, Request, Query
from typing import Optional
from uuid import UUID
from app.services import orders_service

router = APIRouter(prefix="/orders", tags=["Orders"])


@router.get("")
async def get_orders(
    request: Request,
    limit: int = Query(50, ge=1, le=250),
    offset: int = Query(0, ge=0),
    search: Optional[str] = Query(None),
    search_field: Optional[str] = Query(None),
    payment_method: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    sort_field: str = Query("order_date"),
    sort_direction: str = Query("desc")
):
    """
    Get list of orders with filters and pagination

    Query parameters:
    - limit: Number of orders to return (1-250, default 50)
    - offset: Number of orders to skip (default 0)
    - search: Search term
    - search_field: Field to search in (order_number, customer_name, customer_phone)
    - payment_method: Filter by payment method (cash, card, digital)
    - status: Filter by status (completed, cancelled)
    - sort_field: Field to sort by (order_number, order_date, total_amount, customer_name, payment_method)
    - sort_direction: Sort direction (asc, desc)
    """
    return await orders_service.get_orders_list(
        request,
        limit=limit,
        offset=offset,
        search=search,
        search_field=search_field,
        payment_method=payment_method,
        status=status,
        sort_field=sort_field,
        sort_direction=sort_direction
    )


@router.get("/{order_id}")
async def get_order(
    request: Request,
    order_id: UUID
):
    """
    Get single order by ID
    """
    return await orders_service.get_order_by_id(request, order_id)


@router.get("/{order_id}/items")
async def get_order_items(
    request: Request,
    order_id: UUID
):
    """
    Get order items with modifiers
    """
    return await orders_service.get_order_items(request, order_id)

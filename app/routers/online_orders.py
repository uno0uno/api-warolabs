"""
Online Orders Router
Authenticated endpoints for restaurant operators to manage online orders.
"""
from fastapi import APIRouter, Request, Query
from typing import Optional
from app.services import online_orders_service

router = APIRouter(prefix="/online/orders", tags=["Online Orders (Authenticated)"])


@router.get("/")
async def list_online_orders(
    request: Request,
    status: Optional[str] = Query(None, description="Filter by status: pending, confirmed, preparing, delivered, cancelled"),
    limit: int = Query(50, ge=1, le=200, description="Page size"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
):
    """
    List all online orders for the authenticated tenant (delivery, pickup, dine-in).

    Excludes POS orders. Ordered by most recent first.

    **Requires valid session cookie.**
    """
    return await online_orders_service.get_online_orders_list(
        request, status=status, limit=limit, offset=offset
    )

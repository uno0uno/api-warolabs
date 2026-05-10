"""
Online Orders Router
Authenticated endpoints for restaurant operators to manage online orders.
"""
from fastapi import APIRouter, Depends, Request, Query
from typing import Optional, Literal
from uuid import UUID
from pydantic import BaseModel
from app.core.permissions import Module, require_module
from app.services import online_orders_service

router = APIRouter(prefix="/online/orders", tags=["Online Orders (Authenticated)"])


class UpdateOrderStatusRequest(BaseModel):
    new_status: Literal["confirmed", "preparing", "delivered", "completed", "cancelled"]
    reason: Optional[str] = None
    auto_complete: bool = False


@router.get("", dependencies=[Depends(require_module(Module.VENTAS))])
async def list_online_orders(
    request: Request,
    status: Optional[str] = Query(None, description="Filter by status: pending, confirmed, preparing, delivered, cancelled"),
    limit: int = Query(50, ge=1, le=200, description="Page size"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    sort_field: str = Query("order_date", description="Field to sort by: order_number, order_date, scheduled_time, total_amount, status"),
    sort_direction: Literal["asc", "desc"] = Query("desc", description="Sort direction"),
):
    """
    List all online orders for the authenticated tenant (delivery, pickup, dine-in).

    Excludes POS orders. Ordered by most recent first.

    **Requires valid session cookie.**
    """
    return await online_orders_service.get_online_orders_list(
        request, status=status, limit=limit, offset=offset,
        sort_field=sort_field, sort_direction=sort_direction
    )


@router.get("/{order_id}/status-history", dependencies=[Depends(require_module(Module.VENTAS))])
async def get_online_order_status_history(
    request: Request,
    order_id: UUID,
):
    """Return all status transitions for an online order, ordered by change_date ASC."""
    return await online_orders_service.get_order_status_history(request, order_id)


@router.get("/{order_id}", dependencies=[Depends(require_module(Module.VENTAS))])
async def get_online_order_detail(
    request: Request,
    order_id: UUID,
):
    """
    Get full detail of a single online order by ID.

    Returns order header, items with modifiers, and delivery address.
    Excludes POS orders.

    **Requires valid session cookie.**
    """
    return await online_orders_service.get_online_order_by_id(request, order_id)


@router.patch("/{order_id}/status", dependencies=[Depends(require_module(Module.VENTAS))])
async def update_online_order_status(
    request: Request,
    order_id: UUID,
    body: UpdateOrderStatusRequest,
):
    """
    Update the status of an online order and record the transition in history.

    Enforces allowed state machine transitions:
    - pending → confirmed, cancelled
    - confirmed → preparing, cancelled
    - preparing → delivered, cancelled
    - delivered → completed
    - completed → (terminal)
    - cancelled → (terminal)

    Returns 400 for invalid transitions, 404 if order not found.

    **Requires valid session cookie.**
    """
    return await online_orders_service.update_order_status(
        request, order_id, body.new_status, body.reason, body.auto_complete
    )

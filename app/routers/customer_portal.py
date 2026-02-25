"""
Customer Portal Router
Authenticated endpoints for customers to manage their own data.
Authentication: waro_customer_session JWT cookie (set by POST /online/otp/verify)
"""
from fastapi import APIRouter, Depends, Response, Query
from typing import Optional
from uuid import UUID
from app.dependencies.customer_auth import get_current_customer
from app.core.security import clear_customer_cookie
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


@router.post("/logout")
async def customer_logout(response: Response):
    """
    Log out customer by clearing the waro_customer_session cookie.
    """
    clear_customer_cookie(response)
    return {"success": True, "message": "Logged out"}

"""
Customer Portal Router
Authenticated endpoints for customers to manage their own data.
Authentication: waro_customer_session JWT cookie (set by POST /online/otp/verify)
"""
from fastapi import APIRouter, Depends, Response
from app.dependencies.customer_auth import get_current_customer
from app.core.security import clear_customer_cookie

router = APIRouter(prefix="/api/customer", tags=["Customer Portal"])


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


@router.post("/logout")
async def customer_logout(response: Response):
    """
    Log out customer by clearing the waro_customer_session cookie.
    """
    clear_customer_cookie(response)
    return {"success": True, "message": "Logged out"}

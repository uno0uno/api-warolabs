"""
Customer Auth Dependency
Validates the waro_customer_session JWT cookie for /api/customer/* routes.
"""
from fastapi import Request, HTTPException
from app.core.security import validate_jwt_token, CUSTOMER_COOKIE_NAME


async def get_current_customer(request: Request) -> dict:
    """
    FastAPI dependency that validates the waro_customer_session cookie.

    Returns:
        { "customer_id": str, "email": str }

    Raises:
        401 if cookie is missing, expired, or not a customer token
    """
    token = request.cookies.get(CUSTOMER_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Customer session not found")

    payload = validate_jwt_token(token)  # raises 401 on invalid/expired

    if payload.get("type") != "customer":
        raise HTTPException(status_code=401, detail="Invalid session type")

    return {
        "customer_id": payload["customer_id"],
        "email": payload["email"],
    }

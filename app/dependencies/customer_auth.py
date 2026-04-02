"""
Customer Auth Dependency
Validates the waro_customer_session JWT cookie for /api/customer/* routes.
Provides get_customer_flexible for /v1/ endpoints that accept both header and cookie.
"""
from fastapi import Request, HTTPException
from app.core.security import validate_jwt_token, CUSTOMER_COOKIE_NAME

X_CUSTOMER_TOKEN_HEADER = "X-Customer-Token"


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


async def get_customer_flexible(request: Request) -> dict:
    """
    FastAPI dependency that accepts customer auth from:
      1. X-Customer-Token header (takes precedence — for non-browser/API clients)
      2. waro_customer_session cookie (fallback — for browser clients)

    Returns:
        { "customer_id": str, "email": str }

    Raises:
        401 if neither source provides a token, or if token is invalid/expired
    """
    token = request.headers.get(X_CUSTOMER_TOKEN_HEADER) or request.cookies.get(CUSTOMER_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Customer session not found")

    payload = validate_jwt_token(token)  # raises 401 on invalid/expired

    if payload.get("type") != "customer":
        raise HTTPException(status_code=401, detail="Invalid session type")

    return {
        "customer_id": payload["customer_id"],
        "email": payload["email"],
    }

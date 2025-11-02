import jwt
from datetime import datetime, timedelta
from fastapi import Request, HTTPException, Response
from app.config import settings
from typing import Optional

def get_session_token(request: Request) -> str:
    """Extract session-token from cookies - compatible with warolabs.com"""
    session_token = request.cookies.get("session-token")
    if not session_token:
        raise HTTPException(status_code=401, detail="No session found")
    return session_token

def set_session_cookie(response: Response, session_token: str):
    """Set session cookie compatible with warolabs.com"""
    response.set_cookie(
        key="session-token",
        value=session_token,
        httponly=True,
        secure=not settings.is_development,  # Use property from config
        samesite="lax",
        max_age=7 * 24 * 60 * 60  # 7 days
    )

def clear_session_cookie(response: Response):
    """Clear session cookie"""
    response.delete_cookie("session-token")

def validate_jwt_token(token: str) -> dict:
    """Validate JWT using same secret as warolabs.com"""
    try:
        payload = jwt.decode(
            token, 
            settings.jwt_secret,  # Use same secret from .env
            algorithms=["HS256"]
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

def detect_tenant_from_headers(request: Request) -> dict:
    """Port exact tenant detection logic from warolabs.com"""
    return {
        'host': request.headers.get('host', ''),
        'origin': request.headers.get('origin', ''),
        'referer': request.headers.get('referer', ''),
        'forwarded_host': request.headers.get('x-forwarded-host', ''),
        'original_host': request.headers.get('x-original-host', ''),
    }
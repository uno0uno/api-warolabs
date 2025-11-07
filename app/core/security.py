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

def set_session_cookie(response: Response, session_token: str, tenant_site: str = None):
    """Set session cookie with correct domain for the tenant"""
    # Determine cookie domain based on tenant site
    cookie_domain = None
    if not settings.is_development and tenant_site:
        if tenant_site == "warocol.com":
            cookie_domain = ".warocol.com"
        elif tenant_site == "warolabs.com":
            cookie_domain = ".warolabs.com"
    
    response.set_cookie(
        key="session-token",
        value=session_token,
        httponly=True,
        secure=not settings.is_development,
        samesite="none" if not settings.is_development else "lax",
        max_age=7 * 24 * 60 * 60,  # 7 days
        domain=cookie_domain
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

def get_client_ip(request: Request) -> Optional[str]:
    """Get client IP address from request headers"""
    forwarded_for = request.headers.get('x-forwarded-for')
    if forwarded_for:
        return forwarded_for.split(',')[0].strip()
    return request.client.host if request.client else None

def detect_tenant_from_headers(request: Request) -> dict:
    """Port exact tenant detection logic from warolabs.com"""
    return {
        'host': request.headers.get('host', ''),
        'origin': request.headers.get('origin', ''),
        'referer': request.headers.get('referer', ''),
        'forwarded_host': request.headers.get('x-forwarded-host', ''),
        'original_host': request.headers.get('x-original-host', ''),
    }

async def get_session_from_request(request: Request) -> Optional[dict]:
    """
    Get session data from request using session token
    Returns session data with user_id, tenant_id, etc.
    """
    from app.database import get_db_connection
    
    try:
        # Get session token from cookies
        session_token = request.cookies.get("session-token")
        if not session_token:
            return None
        
        async with get_db_connection() as conn:
            # Get session data from database
            session_query = """
                SELECT s.user_id, s.tenant_id, s.expires_at, s.is_active,
                       p.email, p.name
                FROM sessions s
                JOIN profile p ON s.user_id = p.id
                WHERE s.id = $1 
                  AND s.expires_at > NOW()
                  AND s.is_active = true
                LIMIT 1
            """
            session_result = await conn.fetchrow(session_query, session_token)
            
            if not session_result:
                return None
            
            return {
                'user_id': session_result['user_id'],
                'tenant_id': session_result['tenant_id'],
                'email': session_result['email'],
                'name': session_result['name'],
                'expires_at': session_result['expires_at'],
                'is_active': session_result['is_active']
            }
            
    except Exception:
        return None
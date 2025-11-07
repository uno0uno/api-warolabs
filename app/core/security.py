import jwt
from datetime import datetime, timedelta
from fastapi import Request, HTTPException, Response
from app.config import settings
from typing import Optional

async def get_session_token(request: Request) -> str:
    """Extract valid session-token from cookies - validates and cleans up invalid tokens"""
    import logging
    from app.database import get_db_connection
    logger = logging.getLogger(__name__)
    
    # Get raw cookie header to handle multiple session-token cookies
    cookie_header = request.headers.get("cookie", "")
    session_tokens = []
    
    # Parse all session-token cookies from the header
    if cookie_header:
        for cookie_pair in cookie_header.split(";"):
            cookie_pair = cookie_pair.strip()
            if cookie_pair.startswith("session-token="):
                token = cookie_pair.split("=", 1)[1]
                session_tokens.append(token)
    
    # Debug: Log all cookies and found session tokens
    logger.info(f"🍪 Cookies received: {dict(request.cookies)}")
    logger.info(f"🔍 Found {len(session_tokens)} session-token cookies: {session_tokens}")
    
    # If no session tokens found, try the standard way as fallback
    if not session_tokens:
        session_token = request.cookies.get("session-token")
        if not session_token:
            raise HTTPException(status_code=401, detail="No session found")
        return session_token
    
    # Validate each session token and find the valid one
    valid_token = None
    invalid_tokens = []
    
    async with get_db_connection() as conn:
        for token in session_tokens:
            try:
                # Check if session is valid in database
                session_query = """
                    SELECT id FROM sessions 
                    WHERE id = $1 AND expires_at > NOW() AND is_active = true
                    LIMIT 1
                """
                session_result = await conn.fetchrow(session_query, token)
                
                if session_result:
                    valid_token = token
                    logger.info(f"✅ Valid session token found: {token}")
                    break
                else:
                    invalid_tokens.append(token)
                    logger.warning(f"❌ Invalid/expired session token: {token}")
            except Exception as e:
                invalid_tokens.append(token)
                logger.warning(f"❌ Error validating token {token}: {e}")
        
        # Clean up invalid sessions from database
        if invalid_tokens:
            logger.info(f"🧹 Cleaning up {len(invalid_tokens)} invalid session tokens")
            for invalid_token in invalid_tokens:
                try:
                    await conn.execute(
                        "UPDATE sessions SET is_active = false WHERE id = $1",
                        invalid_token
                    )
                except Exception as e:
                    logger.warning(f"Failed to deactivate session {invalid_token}: {e}")
    
    if not valid_token:
        logger.warning("No valid session tokens found")
        raise HTTPException(status_code=401, detail="No valid session found")
    
    logger.info(f"✅ Using valid session token: {valid_token}")
    return valid_token

def set_session_cookie(response: Response, session_token: str, tenant_site: str = None):
    """Set session cookie with correct domain for the tenant - clears previous cookies first"""
    # Determine cookie domain based on tenant site
    cookie_domain = None
    if not settings.is_development and tenant_site:
        if tenant_site == "warocol.com":
            cookie_domain = ".warocol.com"
        elif tenant_site == "warolabs.com":
            cookie_domain = ".warolabs.com"
    
    # Clear any existing session-token cookies first by setting expired ones
    response.delete_cookie("session-token", domain=cookie_domain)
    if cookie_domain:
        response.delete_cookie("session-token")  # Also clear without domain
    
    # Set the new session cookie
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
        # Get session token using improved parsing that handles duplicates
        try:
            session_token = await get_session_token(request)
        except HTTPException:
            return None
        
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
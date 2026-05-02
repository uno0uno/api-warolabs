import jwt
import logging
from datetime import datetime, timedelta
from fastapi import Request, HTTPException, Response
from app.config import settings
from typing import Optional

logger = logging.getLogger(__name__)


async def get_session_token(request: Request) -> str:
    """Extract valid session-token from cookies - validates and cleans up invalid tokens"""
    from app.database import get_db_connection
    
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
    
    # If no session tokens found, try the standard way as fallback
    if not session_tokens:
        session_token = request.cookies.get("session-token")
        if not session_token:
            raise HTTPException(status_code=401, detail="No session found")
        logger.info(f"🍪 Using standard cookie method: {session_token}")
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
                    break
                else:
                    invalid_tokens.append(token)
            except Exception:
                invalid_tokens.append(token)
        
        # Clean up invalid sessions from database
        if invalid_tokens:
            logger.info(f"🧹 Cleaning up {len(invalid_tokens)} invalid session tokens")
            for invalid_token in invalid_tokens:
                try:
                    await conn.execute(
                        "UPDATE sessions SET is_active = false WHERE id = $1",
                        invalid_token
                    )
                except Exception:
                    pass  # Silent cleanup
    
    if not valid_token:
        logger.warning("No valid session tokens found")
        raise HTTPException(status_code=401, detail="No valid session found")
    
    logger.info(f"✅ Using valid session token: {valid_token}")
    return valid_token

async def set_session_cookie(response: Response, session_token: str, tenant_site: str = None):
    """Set session cookie with correct domain for the tenant - clears previous cookies first"""
    
    # Determine cookie domain dynamically from database or parameter
    cookie_domain = None
    if not settings.is_development:
        if tenant_site:
            # Use provided tenant_site
            cookie_domain = f".{tenant_site}"
            logger.info(f"🍪 Setting cookie for provided site: {tenant_site} → domain: {cookie_domain}")
        else:
            # Fallback: get from database using session
            try:
                from app.database import get_db_connection
                async with get_db_connection(use_transaction=False) as conn:
                    site_query = """
                        SELECT ts.site
                        FROM sessions s
                        JOIN tenant_sites ts ON s.tenant_id = ts.tenant_id
                        WHERE s.id = $1 AND s.is_active = true AND ts.is_active = true
                        LIMIT 1
                    """
                    site_result = await conn.fetchrow(site_query, session_token)
                    
                    if site_result and site_result['site']:
                        cookie_domain = f".{site_result['site']}"
                        logger.info(f"🍪 Setting cookie from DB lookup: {site_result['site']} → domain: {cookie_domain}")
                    else:
                        logger.warning(f"🍪 No site found for session {session_token} in database")
            except Exception as e:
                logger.warning(f"🍪 Error getting site from DB: {e}")
    
    logger.info(f"🍪 Final cookie settings - domain: {cookie_domain}, token: {session_token[:8]}...")
    
    # Clear any existing session-token cookies first by setting expired ones
    response.delete_cookie("session-token", domain=cookie_domain)
    if cookie_domain:
        response.delete_cookie("session-token")  # Also clear without domain
    
    # Set the new session cookie with improved proxy compatibility
    response.set_cookie(
        key="session-token",
        value=session_token,
        httponly=True,
        secure=not settings.is_development,
        samesite="lax",  # Use lax for better proxy compatibility across environments
        max_age=7 * 24 * 60 * 60,  # 7 days (1 week)
        domain=cookie_domain,
        path="/"  # Ensure cookie is available for all paths
    )

async def clear_session_cookie(response: Response, session_token: str = None):
    """Clear session cookie with dynamic domain from database"""
    cookie_domain = None
    
    if session_token and not settings.is_development:
        try:
            from app.database import get_db_connection
            async with get_db_connection(use_transaction=False) as conn:
                # Get tenant site from session
                site_query = """
                    SELECT ts.site
                    FROM sessions s
                    JOIN tenant_sites ts ON s.tenant_id = ts.tenant_id
                    WHERE s.id = $1 AND s.is_active = true AND ts.is_active = true
                    LIMIT 1
                """
                site_result = await conn.fetchrow(site_query, session_token)
                
                if site_result and site_result['site']:
                    tenant_site = site_result['site']
                    cookie_domain = f".{tenant_site}"
        except Exception:
            pass  # Silent fallback to no domain
    
    # Clear cookie with domain if found
    response.delete_cookie("session-token", domain=cookie_domain, path="/")
    # Also clear without domain as fallback
    if cookie_domain:
        response.delete_cookie("session-token", path="/")

CUSTOMER_COOKIE_NAME = "waro_customer_session"
CUSTOMER_SESSION_DAYS = 7


def create_customer_jwt(customer_id: str, email: str) -> str:
    """Create a signed JWT for a customer session (stateless, no DB row)"""
    payload = {
        "customer_id": customer_id,
        "email": email,
        "type": "customer",
        "exp": datetime.utcnow() + timedelta(days=CUSTOMER_SESSION_DAYS),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def set_customer_cookie(response: Response, jwt_token: str) -> None:
    """Set the waro_customer_session HttpOnly cookie"""
    response.set_cookie(
        key=CUSTOMER_COOKIE_NAME,
        value=jwt_token,
        httponly=True,
        secure=not settings.is_development,
        samesite="lax",
        max_age=CUSTOMER_SESSION_DAYS * 24 * 60 * 60,
        path="/",
    )


def clear_customer_cookie(response: Response) -> None:
    """Delete the waro_customer_session cookie"""
    response.delete_cookie(key=CUSTOMER_COOKIE_NAME, path="/")


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

async def cleanup_zombie_sessions(conn, limit: int = 100) -> int:
    """
    Clean up zombie sessions (expired but still marked as active).
    Called opportunistically during session validation.
    Returns the number of sessions cleaned up.
    """
    try:
        # Mark expired sessions as inactive with proper end reason
        result = await conn.execute("""
            UPDATE sessions
            SET is_active = false,
                ended_at = NOW(),
                end_reason = 'expired_auto_cleanup'
            WHERE is_active = true
              AND expires_at < NOW()
              AND ended_at IS NULL
            LIMIT $1
        """, limit)

        # Extract count from result (format: "UPDATE N")
        count = int(result.split()[-1]) if result else 0
        if count > 0:
            logger.info(f"🧹 Cleaned up {count} zombie sessions")
        return count
    except Exception as e:
        logger.warning(f"Failed to cleanup zombie sessions: {e}")
        return 0


async def get_session_from_request(request: Request) -> Optional[dict]:
    """
    Get session data from request using session token.
    Returns session data with user_id, tenant_id, etc.
    Also handles cleanup of expired/invalid sessions.
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
            # First, check if session exists at all
            session_check = await conn.fetchrow("""
                SELECT id, expires_at, is_active, ended_at
                FROM sessions
                WHERE id = $1
            """, session_token)

            if session_check:
                # Session exists, check if it's expired or inactive
                is_expired = session_check['expires_at'] < datetime.now(session_check['expires_at'].tzinfo)
                is_inactive = not session_check['is_active']

                if is_expired or is_inactive:
                    # Mark session as ended if not already
                    if session_check['ended_at'] is None:
                        end_reason = 'expired' if is_expired else 'invalidated'
                        await conn.execute("""
                            UPDATE sessions
                            SET is_active = false,
                                ended_at = NOW(),
                                end_reason = $2
                            WHERE id = $1 AND ended_at IS NULL
                        """, session_token, end_reason)
                        logger.info(f"🔒 Marked session {session_token[:8]}... as {end_reason}")

                    # Opportunistically clean up other zombie sessions (1 in 10 chance)
                    import random
                    if random.random() < 0.1:
                        await cleanup_zombie_sessions(conn, limit=50)

                    return None

            # Get valid session data
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

    except Exception as e:
        logger.error(f"❌ Error in get_session_from_request: {e}", exc_info=True)
        return None


async def get_current_user_id(request: Request) -> Optional[str]:
    """Get current user ID from session"""
    session = await get_session_from_request(request)
    return session.get('user_id') if session else None
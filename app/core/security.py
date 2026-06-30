import jwt
import logging
from datetime import datetime, timedelta
from urllib.parse import unquote
from uuid import UUID
from fastapi import Request, HTTPException, Response
from app.config import settings
from app.core.internal_roles import LEGACY_INTERNAL_TEAM_ROLES, is_legacy_internal_team_role
from typing import List, Optional

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "session-token"


def _normalize_session_token(raw: str) -> Optional[str]:
    """Decode cookie value and return canonical UUID string, or None if malformed."""
    token = unquote(raw).strip().strip('"').strip("'")
    try:
        return str(UUID(token))
    except ValueError:
        return None


def _session_token_uuids(token_ids: List[str]) -> List[UUID]:
    """Bind session cookie strings as asyncpg uuid[] (avoids uuid = text errors)."""
    return [UUID(t) for t in token_ids]


def collect_session_tokens(request: Request) -> List[str]:
    """Parse every session-token value from the Cookie header (deduped, order preserved)."""
    cookie_header = request.headers.get("cookie", "")
    raw_tokens: List[str] = []

    if cookie_header:
        for cookie_pair in cookie_header.split(";"):
            cookie_pair = cookie_pair.strip()
            if cookie_pair.startswith(f"{SESSION_COOKIE_NAME}="):
                raw_tokens.append(cookie_pair.split("=", 1)[1])

    if not raw_tokens:
        fallback = request.cookies.get(SESSION_COOKIE_NAME)
        if fallback:
            raw_tokens.append(fallback)

    seen = set()
    unique: List[str] = []
    for raw in raw_tokens:
        token = _normalize_session_token(raw)
        if not token:
            logger.warning(
                "Ignoring malformed %s cookie value (prefix=%s)",
                SESSION_COOKIE_NAME,
                raw[:12],
            )
            continue
        if token not in seen:
            seen.add(token)
            unique.append(token)
    return unique


async def _deactivate_session_tokens(conn, token_ids: List[str]) -> None:
    if not token_ids:
        return
    logger.info(f"🧹 Cleaning up {len(token_ids)} invalid session tokens")
    for token in token_ids:
        try:
            await conn.execute(
                "UPDATE sessions SET is_active = false WHERE id = $1::uuid AND is_active = true",
                UUID(token),
            )
        except Exception:
            pass


async def get_session_token(request: Request) -> str:
    """Extract valid session-token from cookies - validates and cleans up invalid tokens"""
    from app.database import get_db_connection

    session_tokens = collect_session_tokens(request)
    if not session_tokens:
        raise HTTPException(status_code=401, detail="No session found")

    session_uuids = _session_token_uuids(session_tokens)

    async with get_db_connection() as conn:
        # When the browser sends duplicate cookies (e.g. after tenant switch),
        # pick the newest valid session instead of failing on the first stale token.
        session_result = await conn.fetchrow(
            """
            SELECT id FROM sessions
            WHERE id = ANY($1::uuid[])
              AND expires_at > NOW()
              AND is_active = true
            ORDER BY created_at DESC
            LIMIT 1
            """,
            session_uuids,
        )

        if session_result:
            valid_token = str(session_result["id"])
            invalid_tokens = [t for t in session_tokens if t != valid_token]
            await _deactivate_session_tokens(conn, invalid_tokens)
            logger.info(f"✅ Using valid session token: {valid_token}")
            return valid_token

        # Diagnose without deactivating — avoids killing a row on transient mismatch.
        diag_rows = await conn.fetch(
            """
            SELECT id::text AS id,
                   is_active,
                   expires_at,
                   (expires_at > NOW()) AS not_expired
            FROM sessions
            WHERE id = ANY($1::uuid[])
            """,
            session_uuids,
        )
        if diag_rows:
            logger.info(
                "No valid session tokens (candidates=%s)",
                [
                    {
                        "id": r["id"][:8],
                        "is_active": r["is_active"],
                        "not_expired": r["not_expired"],
                    }
                    for r in diag_rows
                ],
            )
        else:
            logger.info(
                "No valid session tokens found (count=%d, prefixes=%s) — not in DB",
                len(session_tokens),
                ", ".join(t[:8] for t in session_tokens[:3]),
            )
        raise HTTPException(status_code=401, detail="No valid session found")

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

    _expire_session_cookie_variants(response, cookie_domain)

    # Set the new session cookie with improved proxy compatibility
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_token,
        httponly=True,
        secure=not settings.is_development,
        samesite="lax",  # Use lax for better proxy compatibility across environments
        max_age=7 * 24 * 60 * 60,  # 7 days (1 week)
        domain=cookie_domain,
        path="/"  # Ensure cookie is available for all paths
    )

def _expire_session_cookie_variants(response: Response, cookie_domain: Optional[str] = None) -> None:
    """Expire session-token for host-only and domain-scoped variants (proxy-safe)."""
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    if cookie_domain:
        response.delete_cookie(SESSION_COOKIE_NAME, domain=cookie_domain, path="/")
    # Some browsers keep a host-only cookie alongside a domain-scoped one.
    response.delete_cookie(SESSION_COOKIE_NAME, path="/", secure=not settings.is_development)


async def clear_session_cookie(response: Response, session_token: str = None):
    """Clear session cookie with dynamic domain from database"""
    cookie_domain = None

    if session_token and not settings.is_development:
        try:
            from app.database import get_db_connection
            async with get_db_connection(use_transaction=False) as conn:
                site_query = """
                    SELECT ts.site
                    FROM sessions s
                    JOIN tenant_sites ts ON s.tenant_id = ts.tenant_id
                    WHERE s.id = $1 AND ts.is_active = true
                    LIMIT 1
                """
                site_result = await conn.fetchrow(site_query, session_token)

                if site_result and site_result['site']:
                    cookie_domain = f".{site_result['site']}"
        except Exception:
            pass

    _expire_session_cookie_variants(response, cookie_domain)

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
        except HTTPException as exc:
            if exc.status_code == 401:
                return None
            raise

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

            # Get valid session data + the user's role on this tenant.
            # LEFT JOIN tenant_members so a session whose user has no
            # active membership row still resolves (role becomes None). When
            # duplicate active rows exist, prefer internal roles over customer.
            # Epic 2 (#164) — required by the require_module() dependency
            # so it can decide log/allow/deny without an extra DB hit.
            session_query = """
                SELECT s.user_id, s.tenant_id, s.expires_at, s.is_active,
                       p.email, p.name,
                       tm.role AS role
                FROM sessions s
                JOIN profile p ON s.user_id = p.id
                LEFT JOIN LATERAL (
                    SELECT role
                    FROM tenant_members tm
                    WHERE tm.user_id = s.user_id
                      AND tm.tenant_id = s.tenant_id
                      AND tm.is_active = true
                    ORDER BY CASE WHEN tm.role = ANY($2::text[]) THEN 0 ELSE 1 END
                    LIMIT 1
                ) tm ON true
                WHERE s.id = $1
                  AND s.expires_at > NOW()
                  AND s.is_active = true
                LIMIT 1
            """
            session_result = await conn.fetchrow(
                session_query,
                session_token,
                list(LEGACY_INTERNAL_TEAM_ROLES),
            )

            if not session_result:
                return None

            resolved_role = session_result['role']
            if resolved_role is not None and not is_legacy_internal_team_role(resolved_role):
                await conn.execute("""
                    UPDATE sessions
                    SET is_active = false,
                        ended_at = NOW(),
                        end_reason = 'customer_role_denied'
                    WHERE id = $1 AND is_active = true
                """, session_token)
                logger.warning(
                    "Denied internal session %s for non-team role %s",
                    session_token[:8],
                    resolved_role,
                )
                return None

            return {
                'user_id': session_result['user_id'],
                'tenant_id': session_result['tenant_id'],
                'email': session_result['email'],
                'name': session_result['name'],
                'expires_at': session_result['expires_at'],
                'is_active': session_result['is_active'],
                'role': resolved_role,
            }

    except Exception as e:
        logger.error(f"❌ Error in get_session_from_request: {e}", exc_info=True)
        return None


async def get_current_user_id(request: Request) -> Optional[str]:
    """Get current user ID from session"""
    session = await get_session_from_request(request)
    return session.get('user_id') if session else None

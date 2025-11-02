import logging
from typing import Optional, Dict, Any
from uuid import UUID
from fastapi import Request, Response
from app.database import get_db_connection
from app.core.security import get_session_token, clear_session_cookie
from app.core.exceptions import AuthenticationError
from app.models.auth import User, Session, Tenant, SessionResponse
from app.core.logging import log_request_context

logger = logging.getLogger(__name__)

async def get_session_data(request: Request, response: Response) -> SessionResponse:
    """
    Port exact session validation logic from warolabs.com/server/api/auth/session.get.js
    """
    try:
        session_token = get_session_token(request)
        
        logger.info(f"Checking session: {session_token[:8]}...")
        
        async with get_db_connection() as conn:
            # Find valid session with analytics tracking (exact query from warolabs.com)
            session_query = """
                SELECT s.*, p.id as user_id, p.email, p.name, p.created_at as user_created_at
                FROM sessions s
                JOIN profile p ON s.user_id = p.id
                WHERE s.id = $1 
                  AND s.expires_at > NOW()
                  AND s.is_active = true
                LIMIT 1
            """
            session_result = await conn.fetchrow(session_query, session_token)
            
            if not session_result:
                logger.warning("Invalid or expired session")
                clear_session_cookie(response)
                raise AuthenticationError("Session expired")
            
            logger.info(f"Valid session found for user: {session_result['user_id']}, tenant: {session_result['tenant_id']}")
            
            # Update last activity for analytics tracking (exact logic from warolabs.com)
            await conn.execute(
                'UPDATE sessions SET last_activity_at = NOW() WHERE id = $1',
                session_token
            )
            logger.info("Session activity updated for analytics")
            
            # Get tenant info if tenant_id exists (exact logic from warolabs.com)
            current_tenant = None
            if session_result['tenant_id']:
                tenant_query = "SELECT id, name, slug FROM tenants WHERE id = $1"
                tenant_result = await conn.fetchrow(tenant_query, session_result['tenant_id'])
                if tenant_result:
                    current_tenant = Tenant(
                        id=tenant_result['id'],
                        name=tenant_result['name'],
                        slug=tenant_result['slug']
                    )
            
            # Build response models
            user = User(
                id=session_result['user_id'],
                email=session_result['email'],
                name=session_result['name'],
                createdAt=session_result['user_created_at']
            )
            
            session = Session(
                expiresAt=session_result['expires_at'],
                createdAt=session_result['created_at'],
                lastActivity=session_result['last_activity_at'],
                ipAddress=str(session_result['ip_address']) if session_result['ip_address'] else None,
                loginMethod=session_result['login_method'],
                tenantId=session_result['tenant_id']
            )
            
            return SessionResponse(
                user=user,
                session=session,
                currentTenant=current_tenant
            )
            
    except AuthenticationError:
        raise
    except Exception as e:
        logger.error(f"Session check error: {e}", exc_info=True)
        raise AuthenticationError("Session validation failed")
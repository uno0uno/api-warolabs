import logging
import secrets
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from uuid import UUID
from fastapi import Request, Response
from app.database import get_db_connection
from app.core.security import get_session_token, clear_session_cookie, set_session_cookie, get_client_ip
from app.core.exceptions import AuthenticationError
from app.core.middleware import require_valid_session
from app.models.auth import User, Session, Tenant, SessionResponse, SwitchTenantResponse
from app.core.logging import log_request_context

logger = logging.getLogger(__name__)

async def get_session_data(request: Request, response: Response) -> SessionResponse:
    """
    Port exact session validation logic from warolabs.com/server/api/auth/session.get.js
    """
    try:
        session_token = await get_session_token(request)
        
        
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
                await clear_session_cookie(response, session_token)
                raise AuthenticationError("Session expired")
            
            
            # Update last activity for analytics tracking (exact logic from warolabs.com)
            await conn.execute(
                'UPDATE sessions SET last_activity_at = NOW() WHERE id = $1',
                session_token
            )
            
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

async def switch_tenant(request: Request, response: Response, tenant_slug: str) -> SwitchTenantResponse:
    """
    Switch to a different tenant for the current user
    Port exact logic from warolabs.com/server/api/auth/switch-tenant.post.js
    """
    try:
        # Get session context from middleware
        session_context = require_valid_session(request)
        current_session_token = await get_session_token(request)
        
        # Get the target site from encrypted origin header
        target_site = None
        encrypted_origin = request.headers.get('x-encrypted-origin')
        if encrypted_origin:
            from app.utils.encryption import decrypt_origin
            target_site = decrypt_origin(encrypted_origin)
            logger.info(f"🔐 Decrypted target site: {target_site}")
        else:
            logger.warning("🔐 No encrypted origin header found")
        
        
        async with get_db_connection() as conn:
            # Get additional session info for new session creation
            current_session_query = """
                SELECT s.ip_address, s.user_agent, s.login_method
                FROM sessions s
                WHERE s.id = $1 
                  AND s.expires_at > NOW()
                  AND s.is_active = true
                LIMIT 1
            """
            current_session_result = await conn.fetchrow(current_session_query, current_session_token)
            
            if not current_session_result:
                logger.warning("Invalid session for tenant switch")
                raise AuthenticationError("Invalid session")
            
            user_id = session_context.user_id
            ip_address = current_session_result['ip_address']
            user_agent = current_session_result['user_agent']
            login_method = current_session_result['login_method']
            
            
            # Validate user has access to requested tenant and get site info
            tenant_access_query = """
                SELECT t.id, t.name, t.slug, ts.site
                FROM tenants t
                INNER JOIN tenant_members tm ON t.id = tm.tenant_id
                LEFT JOIN tenant_sites ts ON t.id = ts.tenant_id AND ts.is_active = true
                WHERE t.slug = $1 AND tm.user_id = $2
                LIMIT 1
            """
            tenant_access_result = await conn.fetchrow(tenant_access_query, tenant_slug, user_id)
            
            if not tenant_access_result:
                logger.warning(f"Access denied to tenant {tenant_slug} for user {user_id}")
                raise AuthenticationError("Access denied to this tenant")
            
            tenant_id = tenant_access_result['id']
            tenant_name = tenant_access_result['name']
            tenant_site = tenant_access_result['site']
            
            # End ALL active sessions for this user to prevent cookie conflicts
            await conn.execute(
                'UPDATE sessions SET is_active = false, ended_at = NOW(), end_reason = $1 WHERE user_id = $2 AND is_active = true',
                'tenant_switch', user_id
            )
            logger.info(f"🧹 Ended all active sessions for user during tenant switch: {user_id}")
            
            # Create new session with new tenant
            new_session_id = secrets.token_hex(16)
            expires_at = datetime.utcnow() + timedelta(days=7)  # 7 days
            
            # Use current client info for new session
            current_client_ip = get_client_ip(request)
            current_user_agent = request.headers.get('user-agent')
            
            session_query = """
                INSERT INTO sessions (
                  id, user_id, tenant_id, expires_at, ip_address, 
                  user_agent, login_method, is_active, created_at, last_activity_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, true, NOW(), NOW())
            """
            await conn.execute(session_query, 
                new_session_id, user_id, tenant_id, expires_at,
                current_client_ip or ip_address,  # Use current or fallback to previous
                current_user_agent or user_agent,  # Use current or fallback to previous
                login_method
            )
            
            # Set new session cookie with correct domain from encrypted origin or fallback to DB
            cookie_site = target_site or tenant_site
            await set_session_cookie(response, new_session_id, cookie_site)
            logger.info(f"🍪 Setting session cookie for site: {cookie_site} (encrypted: {bool(target_site)})")
            
            # Build response
            tenant = Tenant(
                id=tenant_id,
                name=tenant_name,
                slug=tenant_slug
            )
            
            
            return SwitchTenantResponse(
                tenant=tenant,
                timestamp=datetime.utcnow().isoformat()
            )
            
    except AuthenticationError:
        raise
    except Exception as e:
        logger.error(f"❌ Tenant switch error: {e}", exc_info=True)
        raise AuthenticationError("Tenant switch failed")
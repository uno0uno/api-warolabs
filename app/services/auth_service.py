import logging
import secrets
from datetime import datetime, timedelta
from typing import Optional, Set
from fastapi import HTTPException, Request, Response
import asyncpg
from app.config import settings
from app.database import get_db_connection
from app.core.security import (
    INTERNAL_SESSION_HOURS,
    IDLE_SESSION_HOURS,
    collect_session_tokens,
    get_session_token,
    clear_session_cookie,
    set_session_cookie,
    get_client_ip,
)
from app.core.exceptions import AuthenticationError
from app.core.internal_roles import LEGACY_INTERNAL_TEAM_ROLES, is_legacy_internal_team_role
from app.core.onboarding_access import next_step_for_state
from app.core.middleware import require_valid_session
from app.core.platform_superusers import is_platform_superuser_email
from app.models.auth import (
    ProfileAvatarResponse,
    ProfileUser,
    Session,
    SessionResponse,
    SwitchTenantResponse,
    Tenant,
    UpdateProfileResponse,
)
from app.services.aws_s3_service import AWSS3Service

logger = logging.getLogger(__name__)


async def session_cap_for_user(conn, user_id) -> int:
    """Max concurrent active sessions: 2 for superuser (tenant or platform), else 1."""
    email = await conn.fetchval("SELECT email FROM profile WHERE id = $1", user_id)
    if is_platform_superuser_email(email):
        return 2
    has_superuser = await conn.fetchval(
        """
        SELECT 1
        FROM tenant_members
        WHERE user_id = $1
          AND is_active = true
          AND role = 'superuser'
        LIMIT 1
        """,
        user_id,
    )
    return 2 if has_superuser else 1


async def replace_active_admin_sessions(conn, user_id, keep_session_id=None) -> int:
    """
    Enforce concurrent session cap for this user.
    Keeps keep_session_id plus up to (cap - 1) newest other active sessions.
    """
    max_sessions = await session_cap_for_user(conn, user_id)
    # Other active sessions allowed besides keep_session_id
    keep_others = max(max_sessions - 1, 0)
    result = await conn.execute(
        """
        WITH ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (ORDER BY created_at DESC) AS rn
            FROM sessions
            WHERE user_id = $1
              AND is_active = true
              AND expires_at > NOW()
              AND last_activity_at > NOW() - ($4::int * INTERVAL '1 hour')
              AND ($2::uuid IS NULL OR id <> $2::uuid)
        )
        UPDATE sessions s
        SET is_active = false,
            ended_at = NOW(),
            end_reason = 'replaced_by_new_login'
        FROM ranked r
        WHERE s.id = r.id
          AND r.rn > $3
        """,
        user_id,
        keep_session_id,
        keep_others,
        IDLE_SESSION_HOURS,
    )
    count = int(result.split()[-1]) if result else 0
    if count:
        logger.info(
            "Ended %s previous active admin sessions for user %s (cap=%s)",
            count,
            user_id,
            max_sessions,
        )
    return count


async def get_session_data(request: Request, response: Response) -> SessionResponse:
    """
    Port exact session validation logic from warolabs.com/server/api/auth/session.get.js
    """
    try:
        session_token = await get_session_token(request)
        
        
        async with get_db_connection() as conn:
            # Find valid session with analytics tracking (exact query from warolabs.com)
            session_query = """
                SELECT s.*, p.id as user_id, p.email, p.name, p.user_name,
                       p.description, p.logo_avatar, p.preferred_locale,
                       p.pos_catalog_layout_override,
                       p.created_at as user_created_at
                FROM sessions s
                JOIN profile p ON s.user_id = p.id
                WHERE s.id = $1 
                  AND s.expires_at > NOW()
                  AND s.is_active = true
                  AND s.last_activity_at > NOW() - ($2::int * INTERVAL '1 hour')
                LIMIT 1
            """
            try:
                session_result = await conn.fetchrow(
                    session_query, session_token, IDLE_SESSION_HOURS
                )
            except asyncpg.UndefinedColumnError:
                logger.warning(
                    "profile.pos_catalog_layout_override missing; "
                    "using null until warocol.com#2496 migration is applied."
                )
                session_query_legacy = """
                    SELECT s.*, p.id as user_id, p.email, p.name, p.user_name,
                           p.description, p.logo_avatar, p.preferred_locale,
                           p.created_at as user_created_at
                    FROM sessions s
                    JOIN profile p ON s.user_id = p.id
                    WHERE s.id = $1
                      AND s.expires_at > NOW()
                      AND s.is_active = true
                      AND s.last_activity_at > NOW() - ($2::int * INTERVAL '1 hour')
                    LIMIT 1
                """
                session_result = await conn.fetchrow(
                    session_query_legacy, session_token, IDLE_SESSION_HOURS
                )
            
            if not session_result:
                logger.warning("Invalid or expired session")
                await clear_session_cookie(response, session_token)
                raise AuthenticationError("Session expired")

            # Get tenant info if tenant_id exists (exact logic from warolabs.com)
            current_tenant = None
            user_role = None
            lifecycle_status = 'active'
            onboarding_state = None
            if session_result['tenant_id']:
                tenant_query = """
                    SELECT t.id, t.name, t.slug, t.lifecycle_status,
                           o.state AS onboarding_state
                    FROM tenants t
                    LEFT JOIN tenant_onboarding o
                      ON o.tenant_id = t.id
                     AND o.owner_user_id = $2
                    WHERE t.id = $1
                """
                tenant_result = await conn.fetchrow(
                    tenant_query,
                    session_result['tenant_id'],
                    session_result['user_id'],
                )
                if tenant_result:
                    current_tenant = Tenant(
                        id=tenant_result['id'],
                        name=tenant_result['name'],
                        slug=tenant_result['slug']
                    )
                    lifecycle_status = tenant_result.get('lifecycle_status') or 'active'
                    onboarding_state = tenant_result.get('onboarding_state')
                # Only read role from ACTIVE memberships (#201). Customer rows
                # are active memberships, but they are not internal team access.
                role_result = await conn.fetchrow(
                    """
                    SELECT role
                    FROM tenant_members
                    WHERE tenant_id = $1
                      AND user_id = $2
                      AND is_active = true
                    ORDER BY CASE WHEN role = ANY($3::text[]) THEN 0 ELSE 1 END
                    LIMIT 1
                    """,
                    session_result['tenant_id'],
                    session_result['user_id'],
                    list(LEGACY_INTERNAL_TEAM_ROLES),
                )
                if role_result:
                    user_role = role_result['role']

            if is_platform_superuser_email(session_result.get('email')):
                user_role = 'superuser'
            elif user_role is not None and not is_legacy_internal_team_role(user_role):
                await conn.execute(
                    """
                    UPDATE sessions
                    SET is_active = false,
                        ended_at = NOW(),
                        end_reason = 'customer_role_denied'
                    WHERE id = $1 AND is_active = true
                    """,
                    session_token,
                )
                await clear_session_cookie(response, session_token)
                logger.warning(
                    "Denied /auth/session for non-team role %s on session %s",
                    user_role,
                    session_token[:8],
                )
                raise AuthenticationError("Access denied")

            from app.core.security import touch_session_activity
            await touch_session_activity(conn, session_token)

            # Build response models
            user = ProfileUser(
                id=session_result['user_id'],
                email=session_result['email'],
                name=session_result.get('name'),
                user_name=session_result.get('user_name'),
                description=session_result.get('description'),
                logo_avatar=session_result.get('logo_avatar'),
                preferred_locale=session_result.get('preferred_locale'),
                pos_catalog_layout_override=session_result.get('pos_catalog_layout_override'),
                createdAt=session_result.get('user_created_at') or datetime.utcnow(),
                role=user_role
            )
            
            session = Session(
                expiresAt=session_result['expires_at'],
                createdAt=session_result['created_at'],
                ipAddress=str(session_result['ip_address']) if session_result['ip_address'] else None,
                loginMethod=session_result['login_method'],
                tenantId=session_result['tenant_id']
            )
            
            return SessionResponse(
                user=user,
                session=session,
                has_internal_access=is_legacy_internal_team_role(user_role),
                currentTenant=current_tenant,
                lifecycleStatus=lifecycle_status,
                onboardingState=onboarding_state,
                nextStep=next_step_for_state(onboarding_state),
            )
            
    except AuthenticationError:
        raise
    except HTTPException as exc:
        if exc.status_code == 401:
            session_tokens = collect_session_tokens(request)
            await clear_session_cookie(response, session_tokens[0] if session_tokens else None)
            detail = exc.detail if isinstance(exc.detail, str) else "Session expired"
            raise AuthenticationError(detail)
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
        if not is_legacy_internal_team_role(session_context.role):
            if not is_platform_superuser_email(session_context.email):
                raise AuthenticationError("Access denied to this tenant")
        
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
            # Get additional session info including current tenant
            current_session_query = """
                SELECT s.ip_address, s.user_agent, s.login_method, s.tenant_id, t.slug as current_tenant_slug
                FROM sessions s
                LEFT JOIN tenants t ON s.tenant_id = t.id
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
            current_tenant_slug = current_session_result['current_tenant_slug']
            
            # Check if already on the requested tenant
            if current_tenant_slug == tenant_slug:
                logger.info(f"✅ Already on tenant {tenant_slug}, skipping unnecessary switch")
                
                # Return current tenant info without creating new session
                tenant = Tenant(
                    id=current_session_result['tenant_id'],
                    name=tenant_slug,  # We'll get the proper name below
                    slug=tenant_slug
                )
                
                # Get proper tenant name
                tenant_name_query = "SELECT name FROM tenants WHERE slug = $1"
                tenant_name_result = await conn.fetchrow(tenant_name_query, tenant_slug)
                if tenant_name_result:
                    tenant.name = tenant_name_result['name']
                
                return SwitchTenantResponse(
                    tenant=tenant,
                    timestamp=datetime.utcnow().isoformat()
                )
            
            
            # Validate user has an ACTIVE membership in the requested tenant.
            # The `tm.is_active = true` filter is load-bearing: without it,
            # soft-deleted (terminated) members can switch back to a tenant
            # they were removed from. See docs/permissions-router-mapping.md §9.
            # Platform allowlist may switch without a membership row.
            if is_platform_superuser_email(session_context.email):
                tenant_access_result = await conn.fetchrow(
                    """
                    SELECT t.id, t.name, t.slug, ts.site
                    FROM tenants t
                    LEFT JOIN tenant_sites ts ON t.id = ts.tenant_id AND ts.is_active = true
                    WHERE t.slug = $1
                    LIMIT 1
                    """,
                    tenant_slug,
                )
            else:
                tenant_access_result = await conn.fetchrow(
                    """
                    SELECT t.id, t.name, t.slug, ts.site
                    FROM tenants t
                    INNER JOIN tenant_members tm ON t.id = tm.tenant_id
                    LEFT JOIN tenant_sites ts ON t.id = ts.tenant_id AND ts.is_active = true
                    WHERE t.slug = $1
                      AND tm.user_id = $2
                      AND tm.is_active = true
                      AND tm.role = ANY($3::text[])
                    LIMIT 1
                    """,
                    tenant_slug,
                    user_id,
                    list(LEGACY_INTERNAL_TEAM_ROLES),
                )

            if not tenant_access_result:
                logger.warning(f"Access denied to tenant {tenant_slug} for user {user_id}")
                raise AuthenticationError("Access denied to this tenant")

            tenant_id = tenant_access_result['id']
            tenant_name = tenant_access_result['name']
            tenant_site = tenant_access_result['site']

            # End only the CURRENT session (not all sessions)
            await conn.execute(
                'UPDATE sessions SET is_active = false, ended_at = NOW(), end_reason = $1 WHERE id = $2',
                'tenant_switch', current_session_token
            )
            logger.info(f"🔄 Ended current session for tenant switch: {current_session_token}")

            # Create new session with new tenant
            new_session_id = secrets.token_hex(16)
            expires_at = datetime.utcnow() + timedelta(hours=INTERNAL_SESSION_HOURS)  # 24 hours
            
            # Use current client info for new session
            current_client_ip = get_client_ip(request)
            current_user_agent = request.headers.get('user-agent')
            
            session_query = """
                INSERT INTO sessions (
                  id, user_id, tenant_id, expires_at, ip_address,
                  user_agent, login_method, is_active, created_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, true, NOW())
            """
            await conn.execute(session_query, 
                new_session_id, user_id, tenant_id, expires_at,
                current_client_ip or ip_address,  # Use current or fallback to previous
                current_user_agent or user_agent,  # Use current or fallback to previous
                login_method
            )
            await replace_active_admin_sessions(conn, user_id, new_session_id)
            
            # Clear stale cookie variants before issuing the new session (tenant switch).
            cookie_site = target_site or tenant_site
            await clear_session_cookie(response, current_session_token)
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


async def update_profile(
    request: Request,
    name: Optional[str] = None,
    user_name: Optional[str] = None,
    phone_number: Optional[str] = None,
    city: Optional[str] = None,
    description: Optional[str] = None,
    preferred_locale: Optional[str] = None,
    pos_catalog_layout_override: Optional[str] = None,
    fields_set: Optional[Set[str]] = None,
) -> UpdateProfileResponse:
    """
    Update the current user's profile information
    """
    try:
        # Get session context from middleware
        session_context = require_valid_session(request)
        user_id = session_context.user_id

        async with get_db_connection() as conn:
            # Build dynamic update query based on provided fields
            updates = []
            values = []
            param_idx = 1

            provided_fields = fields_set or {
                field_name
                for field_name, field_value in {
                    'name': name,
                    'user_name': user_name,
                    'phone_number': phone_number,
                    'city': city,
                    'description': description,
                    'preferred_locale': preferred_locale,
                    'pos_catalog_layout_override': pos_catalog_layout_override,
                }.items()
                if field_value is not None
            }

            if 'name' in provided_fields and name is not None:
                updates.append(f"name = ${param_idx}")
                values.append(name)
                param_idx += 1

            if 'user_name' in provided_fields and user_name is not None:
                updates.append(f"user_name = ${param_idx}")
                values.append(user_name)
                param_idx += 1

            if 'phone_number' in provided_fields and phone_number is not None:
                updates.append(f"phone_number = ${param_idx}")
                values.append(phone_number)
                param_idx += 1

            if 'city' in provided_fields and city is not None:
                updates.append(f"city = ${param_idx}")
                values.append(city)
                param_idx += 1

            if 'description' in provided_fields:
                updates.append(f"description = ${param_idx}")
                values.append(description)
                param_idx += 1

            if 'preferred_locale' in provided_fields:
                updates.append(f"preferred_locale = ${param_idx}")
                values.append(preferred_locale)
                param_idx += 1

            if 'pos_catalog_layout_override' in provided_fields:
                updates.append(f"pos_catalog_layout_override = ${param_idx}")
                values.append(pos_catalog_layout_override)
                param_idx += 1

            if not updates:
                raise AuthenticationError("No fields to update")

            # Add updated_at
            updates.append("updated_at = NOW()")

            # Add user_id as the last parameter
            values.append(user_id)

            update_query = f"""
                UPDATE profile
                SET {', '.join(updates)}
                WHERE id = ${param_idx}
                RETURNING id, email, name, user_name, description, logo_avatar,
                          preferred_locale, pos_catalog_layout_override, created_at
            """

            try:
                result = await conn.fetchrow(update_query, *values)
            except asyncpg.UndefinedColumnError:
                if 'pos_catalog_layout_override' not in provided_fields:
                    raise
                raise AuthenticationError(
                    "POS catalog layout preference is not available yet"
                ) from None

            if not result:
                raise AuthenticationError("User not found")

            user = ProfileUser(
                id=result['id'],
                email=result['email'],
                name=result['name'],
                user_name=result['user_name'],
                description=result['description'],
                logo_avatar=result['logo_avatar'],
                preferred_locale=result['preferred_locale'],
                pos_catalog_layout_override=result.get('pos_catalog_layout_override'),
                createdAt=result['created_at']
            )

            logger.info(f"✅ Profile updated for user {user_id}")

            return UpdateProfileResponse(
                user=user,
                message="Perfil actualizado exitosamente"
            )

    except AuthenticationError:
        raise
    except Exception as e:
        logger.error(f"❌ Profile update error: {e}", exc_info=True)
        raise AuthenticationError("Error al actualizar perfil")


async def upload_profile_avatar(
    request: Request,
    file_bytes: bytes,
    content_type: str,
) -> ProfileAvatarResponse:
    """Upload and persist an avatar owned by the authenticated user."""
    session_context = require_valid_session(request)
    user_id = session_context.user_id

    s3_service = AWSS3Service()
    public_url = await s3_service.upload_user_avatar(
        file_bytes=file_bytes,
        user_id=str(user_id),
        content_type=content_type,
    )

    if public_url is None:
        if not settings.r2_public_url:
            raise HTTPException(status_code=503, detail="Public image storage is not configured")
        raise HTTPException(status_code=500, detail="Failed to upload avatar")

    try:
        async with get_db_connection() as conn:
            result = await conn.fetchrow(
                """
                UPDATE profile
                SET logo_avatar = $1, updated_at = NOW()
                WHERE id = $2
                RETURNING id
                """,
                public_url,
                user_id,
            )
    except Exception as exc:
        logger.error("Failed to persist avatar for user %s", user_id, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to update avatar") from exc

    if not result:
        raise AuthenticationError("User not found")

    logger.info("Profile avatar updated for user %s", user_id)
    return ProfileAvatarResponse(url=public_url, logo_avatar=public_url)

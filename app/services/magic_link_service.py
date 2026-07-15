import logging
import secrets
import random
from datetime import datetime, timedelta
from typing import Any, Optional
from uuid import UUID
from fastapi import HTTPException, Request, Response
from app.database import get_db_connection
from app.core.security import set_session_cookie, get_client_ip
from app.core.middleware import require_valid_tenant
from app.core.exceptions import AuthenticationError, ValidationError
from app.core.email_utils import normalize_email
from app.core.internal_roles import LEGACY_INTERNAL_TEAM_ROLES
from app.models.auth import User, Tenant, MagicLinkResponse, VerifyCodeResponse, VerifyTokenResponse
from app.models.onboarding import OnboardingStatus
from app.services.auth_service import replace_active_admin_sessions
from app.services.onboarding_service import complete_registration, store_registration_challenge

logger = logging.getLogger(__name__)

_INTERNAL_TEAM_MEMBER_QUERY = """
    SELECT p.id as user_id, p.email, p.name, tm.role, tm.tenant_id
    FROM profile p
    INNER JOIN tenant_members tm ON p.id = tm.user_id
    WHERE lower(trim(p.email)) = $1
      AND tm.is_active = true
      AND tm.role = ANY($2::text[])
    ORDER BY tm.id ASC
    LIMIT 1
"""


async def _lookup_internal_team_member(conn, email: str) -> Optional[Any]:
    """First active internal tenant_members row for this email (any tenant)."""
    return await conn.fetchrow(
        _INTERNAL_TEAM_MEMBER_QUERY,
        email,
        list(LEGACY_INTERNAL_TEAM_ROLES),
    )


async def _tenant_email_branding(conn, tenant_id: UUID) -> dict[str, str]:
    row = await conn.fetchrow(
        """
        SELECT
            t.name AS tenant_name,
            COALESCE(t.email, '') AS tenant_email,
            COALESCE(ts.brand_name, t.name) AS brand_name
        FROM tenants t
        LEFT JOIN tenant_sites ts ON ts.tenant_id = t.id AND ts.is_active = true
        WHERE t.id = $1
        ORDER BY ts.created_at ASC NULLS LAST
        LIMIT 1
        """,
        tenant_id,
    )
    if not row:
        return {"tenant_name": "WARO", "tenant_email": "", "brand_name": "WARO"}
    return dict(row)


async def _complete_registration_login(
    conn,
    *,
    request: Request,
    response: Response,
    tenant_context,
    email: str,
    credential: str,
    kind: str,
    login_method: str,
) -> Optional[tuple[User, Tenant, Optional[OnboardingStatus]]]:
    identity = await complete_registration(
        conn,
        email=email,
        credential=credential,
        kind=kind,
    )
    if not identity:
        return None

    session_id = secrets.token_hex(16)
    expires_at = datetime.utcnow() + timedelta(days=7)
    await conn.execute(
        """
        INSERT INTO sessions (
          id, user_id, tenant_id, expires_at, created_at,
          ip_address, user_agent, login_method, is_active
        )
        VALUES ($1, $2, $3, $4, NOW(), $5, $6, $7, true)
        """,
        session_id,
        identity["user_id"],
        identity["tenant_id"],
        expires_at,
        get_client_ip(request),
        request.headers.get("user-agent"),
        login_method,
    )
    await replace_active_admin_sessions(conn, identity["user_id"], session_id)
    await set_session_cookie(response, session_id, tenant_context.site)

    user = User(
        id=identity["user_id"],
        email=identity["email"],
        name=identity.get("name"),
        createdAt=identity.get("user_created_at") or datetime.utcnow(),
    )
    tenant = Tenant(
        id=identity["tenant_id"],
        name=identity["tenant_name"],
        slug=identity["tenant_slug"],
    )
    onboarding = None
    if identity.get("onboarding_state"):
        onboarding = OnboardingStatus(
            tenantId=identity["tenant_id"],
            lifecycleStatus=identity["lifecycle_status"],
            state=identity["onboarding_state"],
            nextStep=identity.get("next_step"),
            emailVerifiedAt=identity.get("email_verified_at"),
        )
    return user, tenant, onboarding

async def send_magic_link(request: Request, email: str, redirect: Optional[str] = None) -> MagicLinkResponse:
    """
    Send magic link using tenant context from middleware
    """
    try:
        email = normalize_email(email)
        # Get validated tenant context from middleware
        tenant_context = require_valid_tenant(request)
        
        logger.info(f"📧 Magic link request for {email} from {tenant_context.site}")
        logger.info(f"🏷️ Using tenant: {tenant_context.tenant_name} (ID: {tenant_context.tenant_id})")
        
        async with get_db_connection() as conn:
            # Generate secure token and verification code
            token = secrets.token_hex(32)
            verification_code = str(random.randint(100000, 999999))  # 6-digit code
            expires_at = datetime.utcnow() + timedelta(minutes=15)  # 15 minutes

            user_result = await _lookup_internal_team_member(conn, email)

            is_registration = user_result is None
            if is_registration:
                should_send = await store_registration_challenge(
                    conn,
                    email=email,
                    token=token,
                    code=verification_code,
                    request_ip=get_client_ip(request),
                    user_agent=request.headers.get("user-agent"),
                )
                if not should_send:
                    return MagicLinkResponse()
                user_tenant_id = tenant_context.tenant_id
                logger.info("Pre-registration verification challenge created")
            else:
                user_id = user_result['user_id']
                user_tenant_id = user_result['tenant_id']
                logger.info(f"✅ User found with ID: {user_id}, role: {user_result['role']}, tenant: {user_tenant_id}")

                # Mark old unused magic tokens as expired for this user
                await conn.execute(
                    'UPDATE magic_tokens SET used = true, used_at = NOW() WHERE user_id = $1 AND used = false',
                    user_id
                )

                # Save new magic token to database with user's tenant_id
                insert_token_query = """
                    INSERT INTO magic_tokens (user_id, token, verification_code, expires_at, tenant_id, used, created_at, used_at)
                    VALUES ($1, $2, $3, $4, $5, false, NOW(), NULL)
                """
                await conn.execute(insert_token_query,
                    user_id, token, verification_code, expires_at, user_tenant_id
                )
                logger.info(f"🔑 Magic token saved for user: {user_id}, tenant: {user_tenant_id}")
            
            # Send magic link email using AWS SES
            from app.services.aws_ses_service import ses_service
            from app.services.email_sender import resolve_sender_email_value
            from app.templates.magic_link_template import get_magic_link_template, get_magic_link_subject
            from app.config import settings
            
            # Generate magic link URL based on detected tenant site
            if settings.is_development:
                # In development, use the requesting site from Origin header
                # This allows mobile/local network access to work properly
                origin = request.headers.get('origin', 'http://localhost:8080')
                base_url = origin
                logger.info(f"🔗 Using origin URL for magic link: {base_url}")
            else:
                # In production, use the detected tenant site from middleware
                base_url = f"https://{tenant_context.site}"
            
            magic_link_url = f"{base_url}/auth/verify?token={token}&email={email}"
            if redirect:
                magic_link_url += f"&redirect={redirect}"
            
            if is_registration:
                brand_name = tenant_context.brand_name or "WARO"
                tenant_name = tenant_context.tenant_name or "WARO"
                tenant_email = tenant_context.tenant_email
            else:
                member_branding = await _tenant_email_branding(conn, user_tenant_id)
                brand_name = member_branding["brand_name"]
                tenant_name = member_branding["tenant_name"]
                tenant_email = member_branding["tenant_email"] or tenant_context.tenant_email

            # Prepare tenant context for template (use the member's restaurant, not the site tenant)
            template_context = {
                'brand_name': brand_name,
                'tenant_name': tenant_name,
                'admin_name': 'Saifer 101 (Anderson Arévalo)',  # Default admin
                'admin_email': tenant_email,
            }
            
            # Generate email content
            html_template = get_magic_link_template(magic_link_url, verification_code, template_context)
            subject = get_magic_link_subject(brand_name)
            
            # Determine sender name with enterprise branding
            from_name = f"Saifer 101 (Anderson Arévalo) - {brand_name}"
            
            # Send email via AWS SES
            email_sent = await ses_service.send_email(
                from_email=resolve_sender_email_value(tenant_email),
                from_name=from_name,
                to_emails=[email],
                subject=subject,
                html_body=html_template
            )
            
            if email_sent:
                logger.info(f"✅ Magic link email sent to {email} from configured SES sender")
                logger.info(f"🔗 Magic link URL: {magic_link_url}")
            else:
                logger.error(f"❌ Failed to send magic link email to {email}")
                # In case of email failure, still log the code for development
                logger.info(f"🔢 FALLBACK: Verification code for {email}: {verification_code}")
            
            logger.info(f"📧 Email sender: {from_name}")
            logger.info(f"🏢 Brand: {brand_name}")
            
            return MagicLinkResponse()
            
    except (ValidationError, AuthenticationError, HTTPException):
        raise
    except Exception as e:
        logger.error(f"❌ Magic link handler error: {e}", exc_info=True)
        raise ValidationError("Failed to send magic link")


async def verify_code(request: Request, response: Response, email: str, code: str) -> VerifyCodeResponse:
    """
    Verify magic link code using tenant context from middleware
    """
    try:
        email = normalize_email(email)
        # Get validated tenant context from middleware
        tenant_context = require_valid_tenant(request)
        
        logger.info(f"🔢 Verification request for {email} from {tenant_context.site}")
        
        async with get_db_connection() as conn:
            # Verify code and get user info
            verify_query = """
                SELECT mt.*, p.email, p.name, p.id as user_id, p.created_at as user_created_at,
                       tm.role as user_role
                FROM magic_tokens mt
                JOIN profile p ON mt.user_id = p.id
                INNER JOIN tenant_members tm ON tm.user_id = p.id AND tm.tenant_id = mt.tenant_id
                WHERE lower(trim(p.email)) = $1 AND mt.verification_code = $2
                AND mt.expires_at > NOW() AND mt.used = false
                AND tm.is_active = true
                AND tm.role = ANY($3::text[])
                LIMIT 1
            """

            token_data = await conn.fetchrow(
                verify_query,
                email,
                code,
                list(LEGACY_INTERNAL_TEAM_ROLES),
            )
            
            if not token_data:
                registration = await _complete_registration_login(
                    conn,
                    request=request,
                    response=response,
                    tenant_context=tenant_context,
                    email=email,
                    credential=code,
                    kind="code",
                    login_method="onboarding_code",
                )
                if not registration:
                    logger.warning(f"❌ Invalid verification code for {email} on {tenant_context.site}")
                    raise AuthenticationError("Invalid or expired verification code")
                user, tenant, onboarding = registration
                return VerifyCodeResponse(user=user, tenant=tenant, onboarding=onboarding)
            
            logger.info(f"✅ Valid verification code for user: {token_data['user_id']}")
            
            # Mark token as used
            await conn.execute(
                'UPDATE magic_tokens SET used = true, used_at = NOW() WHERE verification_code = $1 AND user_id = $2',
                code, token_data['user_id']
            )
            logger.info("✅ Verification code marked as used")

            # Create session with user's tenant from token
            session_id = secrets.token_hex(16)
            expires_at = datetime.utcnow() + timedelta(days=7)  # 7 days (1 week)
            user_tenant_id = token_data['tenant_id']

            # Get client info for analytics
            client_ip = get_client_ip(request)
            user_agent = request.headers.get('user-agent')

            session_query = """
                INSERT INTO sessions (
                  id, user_id, tenant_id, expires_at,
                  created_at,
                  ip_address, user_agent, login_method, is_active
                )
                VALUES ($1, $2, $3, $4, NOW(), $5, $6, 'verification_code', true)
                RETURNING id
            """
            await conn.execute(session_query,
                session_id, token_data['user_id'], user_tenant_id,
                expires_at, client_ip, user_agent
            )
            await replace_active_admin_sessions(conn, token_data['user_id'], session_id)
            logger.info(f"🎫 Session created: {session_id} for tenant: {user_tenant_id}")
            
            # Get tenant info for response
            tenant_query = "SELECT id, name, slug FROM tenants WHERE id = $1"
            tenant_data = await conn.fetchrow(tenant_query, user_tenant_id)

            # Set session cookie with correct domain for tenant
            await set_session_cookie(response, session_id, tenant_context.site)

            # Build response models
            user = User(
                id=token_data['user_id'],
                email=token_data['email'],
                name=token_data['name'],
                createdAt=token_data['user_created_at'] or datetime.utcnow()
            )

            tenant = Tenant(
                id=tenant_data['id'],
                name=tenant_data['name'],
                slug=tenant_data['slug']
            )

            logger.info(f"✅ Verification successful for {email}, tenant: {tenant_data['name']}")
            
            # Send Discord notification
            try:
                from app.services.discord_service import discord_session_service
                if discord_session_service:
                    await discord_session_service.notify_new_session(
                        user_email=email,
                        user_name=token_data['name'],
                        tenant_name=tenant_data['name'],
                        login_method="verification_code",
                        ip_address=client_ip,
                        user_agent=user_agent
                    )
            except Exception as e:
                logger.error(f"Failed to send Discord notification: {e}")

            return VerifyCodeResponse(user=user, tenant=tenant)
            
    except (ValidationError, AuthenticationError, HTTPException):
        raise
    except Exception as e:
        logger.error(f"❌ Verification code handler error: {e}", exc_info=True)
        raise AuthenticationError("Verification failed")


async def verify_token(request: Request, response: Response, email: str, token: str) -> VerifyTokenResponse:
    """
    Verify magic link token using tenant context from middleware
    """
    try:
        email = normalize_email(email)
        # Get validated tenant context from middleware
        tenant_context = require_valid_tenant(request)
        
        logger.info(f"🔍 Token verification request for {email} from {tenant_context.site}")
        
        async with get_db_connection() as conn:
            # Find valid unused magic token
            verify_query = """
                SELECT mt.*, p.email, p.name, p.id as user_id, p.created_at as user_created_at,
                       tm.role as user_role
                FROM magic_tokens mt
                JOIN profile p ON mt.user_id = p.id
                INNER JOIN tenant_members tm ON tm.user_id = p.id AND tm.tenant_id = mt.tenant_id
                WHERE lower(trim(p.email)) = $1 AND mt.token = $2
                AND mt.expires_at > NOW() AND mt.used = false
                AND tm.is_active = true
                AND tm.role = ANY($3::text[])
                LIMIT 1
            """

            token_data = await conn.fetchrow(
                verify_query,
                email,
                token,
                list(LEGACY_INTERNAL_TEAM_ROLES),
            )
            
            if not token_data:
                registration = await _complete_registration_login(
                    conn,
                    request=request,
                    response=response,
                    tenant_context=tenant_context,
                    email=email,
                    credential=token,
                    kind="token",
                    login_method="onboarding_magic_link",
                )
                if not registration:
                    logger.warning(f"❌ Invalid or expired token for {email} on {tenant_context.site}")
                    raise AuthenticationError("Invalid or expired token")
                user, tenant, onboarding = registration
                return VerifyTokenResponse(user=user, tenant=tenant, onboarding=onboarding)
            
            logger.info(f"✅ Valid token found for user: {token_data['user_id']}")
            
            # Mark token as used
            await conn.execute(
                'UPDATE magic_tokens SET used = true, used_at = NOW() WHERE token = $1 AND user_id = $2',
                token, token_data['user_id']
            )
            logger.info("✅ Token marked as used")

            # Create session with user's tenant from token
            session_id = secrets.token_hex(16)
            expires_at = datetime.utcnow() + timedelta(days=7)  # 7 days (1 week)
            user_tenant_id = token_data['tenant_id']

            # Get client info for analytics
            client_ip = get_client_ip(request)
            user_agent = request.headers.get('user-agent')

            session_query = """
                INSERT INTO sessions (
                  id, user_id, tenant_id, expires_at,
                  created_at,
                  ip_address, user_agent, login_method, is_active
                )
                VALUES ($1, $2, $3, $4, NOW(), $5, $6, 'magic_link', true)
                RETURNING id
            """
            await conn.execute(session_query,
                session_id, token_data['user_id'], user_tenant_id,
                expires_at, client_ip, user_agent
            )
            await replace_active_admin_sessions(conn, token_data['user_id'], session_id)
            logger.info(f"🎫 Session created: {session_id} for tenant: {user_tenant_id}")
            
            # Get tenant info for notification
            tenant_query = "SELECT name FROM tenants WHERE id = $1"
            tenant_data = await conn.fetchrow(tenant_query, user_tenant_id)
            tenant_name = tenant_data['name'] if tenant_data else "Unknown Tenant"

            # Set session cookie with correct domain for tenant
            await set_session_cookie(response, session_id, tenant_context.site)

            # Build response model
            user = User(
                id=token_data['user_id'],
                email=token_data['email'],
                name=token_data['name'],
                createdAt=token_data['user_created_at'] or datetime.utcnow()
            )

            logger.info(f"✅ Token verification successful for {email}, tenant: {user_tenant_id}")
            
            # Send Discord notification
            try:
                from app.services.discord_service import discord_session_service
                if discord_session_service:
                    await discord_session_service.notify_new_session(
                        user_email=email,
                        user_name=token_data['name'],
                        tenant_name=tenant_name,
                        login_method="magic_link",
                        ip_address=client_ip,
                        user_agent=user_agent
                    )
            except Exception as e:
                logger.error(f"Failed to send Discord notification: {e}")
            
            return VerifyTokenResponse(user=user)
            
    except (ValidationError, AuthenticationError, HTTPException):
        raise
    except Exception as e:
        logger.error(f"❌ Token verification handler error: {e}", exc_info=True)
        raise AuthenticationError("Token verification failed")

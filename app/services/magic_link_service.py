import asyncio
import logging
import secrets
import random
from datetime import datetime, timedelta
from typing import Any, Optional
from uuid import UUID
from urllib.parse import urlencode
from fastapi import HTTPException, Request, Response
from app.database import get_db_connection
from app.core.security import set_session_cookie, get_client_ip
from app.core.middleware import require_valid_tenant
from app.core.exceptions import AuthenticationError, ValidationError
from app.core.email_utils import normalize_email
from app.core.internal_roles import LEGACY_INTERNAL_TEAM_ROLES
from app.core.onboarding_access import next_step_for_state
from app.models.auth import (
    MagicLinkResponse,
    RegistrationAttribution,
    RegistrationMagicLinkRequest,
    Tenant,
    User,
    VerifyCodeResponse,
    VerifyTokenResponse,
)
from app.models.onboarding import OnboardingStatus
from app.services.auth_service import replace_active_admin_sessions
from app.services.onboarding_service import (
    complete_registration,
    get_resumable_registration_draft,
    store_registration_challenge,
)

logger = logging.getLogger(__name__)

_LOGIN_IDENTITY_QUERY = """
    SELECT p.id as user_id, p.email, p.name, tm.role, tm.tenant_id,
           t.lifecycle_status, o.state AS onboarding_state
    FROM profile p
    INNER JOIN tenant_members tm ON p.id = tm.user_id
    INNER JOIN tenants t ON t.id = tm.tenant_id
    LEFT JOIN tenant_onboarding o
      ON o.tenant_id = t.id AND o.owner_user_id = p.id
    WHERE lower(trim(p.email)) = $1
      AND (
        (tm.is_active = true AND tm.role = ANY($2::text[]))
        OR (
          t.lifecycle_status = 'pending'
          AND tm.role = 'owner'
          AND o.state NOT IN ('setup_complete', 'cancelled')
        )
      )
    ORDER BY tm.is_active DESC, tm.id ASC
    LIMIT 1
"""


async def _lookup_internal_team_member(conn, email: str) -> Optional[Any]:
    """Resolve an existing active member or resumable pending owner."""
    return await conn.fetchrow(
        _LOGIN_IDENTITY_QUERY,
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


def _magic_link_base_url(request: Request, tenant_context) -> str:
    from app.config import settings

    if settings.is_development:
        return request.headers.get("origin", "http://localhost:8080")
    return f"https://{tenant_context.site}"


async def _deliver_magic_link(
    *,
    email: str,
    magic_link_url: str,
    verification_code: str,
    brand_name: str,
    tenant_name: str,
    tenant_email: Optional[str],
) -> None:
    from app.services.aws_ses_service import ses_service
    from app.services.email_sender import resolve_sender_email_value
    from app.templates.magic_link_template import get_magic_link_subject, get_magic_link_template

    template_context = {
        "brand_name": brand_name,
        "tenant_name": tenant_name,
        "admin_name": "Saifer 101 (Anderson Arévalo)",
        "admin_email": tenant_email,
    }
    sent = await ses_service.send_email(
        from_email=resolve_sender_email_value(tenant_email),
        from_name=f"Saifer 101 (Anderson Arévalo) - {brand_name}",
        to_emails=[email],
        subject=get_magic_link_subject(brand_name),
        html_body=get_magic_link_template(
            magic_link_url,
            verification_code,
            template_context,
        ),
    )
    if sent:
        logger.info("Magic link email sent")
    else:
        logger.error("Magic link email delivery failed")


async def _issue_registration_challenge(
    request: Request,
    tenant_context,
    *,
    email: str,
    draft: dict[str, Any],
) -> None:
    """Replace a registration challenge while preserving its consented draft."""
    token = secrets.token_hex(32)
    verification_code = str(random.randint(100000, 999999))
    async with get_db_connection() as conn:
        await store_registration_challenge(
            conn,
            email=email,
            token=token,
            code=verification_code,
            request_ip=get_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            phone_country_code=draft.get("phone_country_code"),
            phone_number=draft.get("phone_number"),
            consent=True,
            business_name=draft.get("business_name"),
            country_code=draft.get("country_code"),
            base_currency_code=draft.get("base_currency_code"),
            source=draft.get("last_source") or draft.get("first_source"),
            content=draft.get("last_content") or draft.get("first_content"),
            campaign=draft.get("last_campaign") or draft.get("first_campaign"),
            variant=draft.get("last_variant") or draft.get("first_variant"),
        )

    query = {"token": token, "purpose": "registration"}
    await _deliver_magic_link(
        email=email,
        magic_link_url=f"{_magic_link_base_url(request, tenant_context)}/auth/verify?{urlencode(query)}",
        verification_code=verification_code,
        brand_name=tenant_context.brand_name or "WARO",
        tenant_name=tenant_context.tenant_name or "WARO",
        tenant_email=tenant_context.tenant_email,
    )


async def _complete_registration_login(
    conn,
    *,
    request: Request,
    response: Response,
    tenant_context,
    email: Optional[str],
    credential: str,
    kind: str,
    login_method: str,
    opaque_token: bool = False,
    legacy_only: bool = False,
) -> Optional[tuple[User, Tenant, Optional[OnboardingStatus]]]:
    identity = await complete_registration(
        conn,
        email=email,
        credential=credential,
        kind=kind,
        opaque_token=opaque_token,
        legacy_only=legacy_only,
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

    notification = identity.pop("registration_notification", None)
    request.state.registration_notification = notification
    request.state.registration_attribution = _registration_attribution_from(notification)

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
            businessName=identity["tenant_name"],
            state=identity["onboarding_state"],
            nextStep=identity.get("next_step"),
            emailVerifiedAt=identity.get("email_verified_at"),
        )
    return user, tenant, onboarding

async def send_magic_link(request: Request, email: str, redirect: Optional[str] = None) -> MagicLinkResponse:
    """Send access for an existing identity or a consented pending registration."""
    try:
        email = normalize_email(email)
        tenant_context = require_valid_tenant(request)

        async with get_db_connection() as conn:
            user_result = await _lookup_internal_team_member(conn, email)
            if not user_result:
                registration_draft = await get_resumable_registration_draft(conn, email)
            else:
                registration_draft = None

            if not user_result and not registration_draft:
                return MagicLinkResponse()

            if not registration_draft:
                token = secrets.token_hex(32)
                verification_code = str(random.randint(100000, 999999))
                expires_at = datetime.utcnow() + timedelta(minutes=15)
                user_id = user_result["user_id"]
                user_tenant_id = user_result["tenant_id"]
                await conn.execute(
                    """
                    UPDATE magic_tokens SET used = true, used_at = NOW()
                    WHERE user_id = $1 AND used = false AND purpose = 'login'
                    """,
                    user_id,
                )
                await conn.execute(
                    """
                    INSERT INTO magic_tokens (
                        user_id, token, verification_code, expires_at, tenant_id,
                        used, created_at, used_at, purpose
                    )
                    VALUES ($1, $2, $3, $4, $5, false, NOW(), NULL, 'login')
                    """,
                    user_id,
                    token,
                    verification_code,
                    expires_at,
                    user_tenant_id,
                )
                branding = await _tenant_email_branding(conn, user_tenant_id)

        if registration_draft:
            # The public response stays generic. A fresh opaque registration
            # challenge is issued only when consented draft data still exists.
            try:
                await _issue_registration_challenge(
                    request,
                    tenant_context,
                    email=email,
                    draft=registration_draft,
                )
            except HTTPException as exc:
                if exc.status_code != 429:
                    raise
            return MagicLinkResponse()

        query = {"token": token, "email": email}
        if redirect:
            query["redirect"] = redirect
        await _deliver_magic_link(
            email=email,
            magic_link_url=f"{_magic_link_base_url(request, tenant_context)}/auth/verify?{urlencode(query)}",
            verification_code=verification_code,
            brand_name=branding["brand_name"],
            tenant_name=branding["tenant_name"],
            tenant_email=branding["tenant_email"] or tenant_context.tenant_email,
        )
        return MagicLinkResponse()
            
    except (ValidationError, AuthenticationError, HTTPException):
        raise
    except Exception as e:
        logger.error(f"❌ Magic link handler error: {e}", exc_info=True)
        raise ValidationError("Failed to send magic link")


async def send_registration_magic_link(
    request: Request,
    payload: RegistrationMagicLinkRequest,
) -> MagicLinkResponse:
    """Start registration, or send login access when the identity already exists."""
    try:
        email = normalize_email(str(payload.email))
        tenant_context = require_valid_tenant(request)
        async with get_db_connection() as conn:
            existing = await _lookup_internal_team_member(conn, email)
        if existing:
            return await send_magic_link(request, email)

        await _issue_registration_challenge(
            request,
            tenant_context,
            email=email,
            draft={
                "phone_country_code": payload.phone_country_code,
                "phone_number": payload.phone_number,
                "business_name": payload.business_name,
                "country_code": payload.country_code,
                "base_currency_code": payload.base_currency_code,
                "last_source": payload.source,
                "last_content": payload.content,
                "last_campaign": payload.campaign,
                "last_variant": payload.variant,
            },
        )
        return MagicLinkResponse()
    except (ValidationError, AuthenticationError, HTTPException):
        raise
    except Exception as exc:
        logger.error("Registration magic link handler failed", exc_info=True)
        raise ValidationError("Failed to send magic link") from exc


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
                       tm.role as user_role, t.lifecycle_status,
                       o.state AS onboarding_state, o.email_verified_at
                FROM magic_tokens mt
                JOIN profile p ON mt.user_id = p.id
                INNER JOIN tenant_members tm ON tm.user_id = p.id AND tm.tenant_id = mt.tenant_id
                INNER JOIN tenants t ON t.id = mt.tenant_id
                LEFT JOIN tenant_onboarding o
                  ON o.tenant_id = t.id AND o.owner_user_id = p.id
                WHERE lower(trim(p.email)) = $1 AND mt.verification_code = $2
                AND mt.expires_at > NOW() AND mt.used = false
                AND mt.purpose = 'login'
                AND (
                    (tm.is_active = true AND tm.role = ANY($3::text[]))
                    OR (
                        t.lifecycle_status = 'pending'
                        AND tm.role = 'owner'
                        AND o.state NOT IN ('setup_complete', 'cancelled')
                    )
                )
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
                    legacy_only=True,
                )
                if not registration:
                    logger.warning(f"❌ Invalid verification code for {email} on {tenant_context.site}")
                    raise AuthenticationError("Invalid or expired verification code")
                user, tenant, onboarding = registration
                _schedule_registration_notification(request)
                return VerifyCodeResponse(user=user, tenant=tenant, onboarding=onboarding)
            
            logger.info(f"✅ Valid verification code for user: {token_data['user_id']}")
            
            # Mark token as used
            await conn.execute(
                """UPDATE magic_tokens SET used = true, used_at = NOW()
                   WHERE verification_code = $1 AND user_id = $2 AND purpose = 'login'""",
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

            onboarding = None
            if token_data.get("onboarding_state"):
                onboarding = OnboardingStatus(
                    tenantId=user_tenant_id,
                    lifecycleStatus=token_data["lifecycle_status"],
                    businessName=tenant_data["name"],
                    state=token_data["onboarding_state"],
                    nextStep=next_step_for_state(token_data["onboarding_state"]),
                    emailVerifiedAt=token_data.get("email_verified_at"),
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

            return VerifyCodeResponse(user=user, tenant=tenant, onboarding=onboarding)
            
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
                       tm.role as user_role, t.lifecycle_status,
                       o.state AS onboarding_state, o.email_verified_at
                FROM magic_tokens mt
                JOIN profile p ON mt.user_id = p.id
                INNER JOIN tenant_members tm ON tm.user_id = p.id AND tm.tenant_id = mt.tenant_id
                INNER JOIN tenants t ON t.id = mt.tenant_id
                LEFT JOIN tenant_onboarding o
                  ON o.tenant_id = t.id AND o.owner_user_id = p.id
                WHERE lower(trim(p.email)) = $1 AND mt.token = $2
                AND mt.expires_at > NOW() AND mt.used = false
                AND mt.purpose = 'login'
                AND (
                    (tm.is_active = true AND tm.role = ANY($3::text[]))
                    OR (
                        t.lifecycle_status = 'pending'
                        AND tm.role = 'owner'
                        AND o.state NOT IN ('setup_complete', 'cancelled')
                    )
                )
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
                    legacy_only=True,
                )
                if not registration:
                    logger.warning(f"❌ Invalid or expired token for {email} on {tenant_context.site}")
                    raise AuthenticationError("Invalid or expired token")
                user, tenant, onboarding = registration
                _schedule_registration_notification(request)
                return VerifyTokenResponse(user=user, tenant=tenant, onboarding=onboarding)
            
            logger.info(f"✅ Valid token found for user: {token_data['user_id']}")
            
            # Mark token as used
            await conn.execute(
                """UPDATE magic_tokens SET used = true, used_at = NOW()
                   WHERE token = $1 AND user_id = $2 AND purpose = 'login'""",
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
            tenant_query = "SELECT id, name, slug FROM tenants WHERE id = $1"
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

            tenant = None
            onboarding = None
            if tenant_data:
                tenant = Tenant(
                    id=tenant_data["id"],
                    name=tenant_data["name"],
                    slug=tenant_data["slug"],
                )
            if token_data.get("onboarding_state") and tenant_data:
                onboarding = OnboardingStatus(
                    tenantId=user_tenant_id,
                    lifecycleStatus=token_data["lifecycle_status"],
                    businessName=tenant_data["name"],
                    state=token_data["onboarding_state"],
                    nextStep=next_step_for_state(token_data["onboarding_state"]),
                    emailVerifiedAt=token_data.get("email_verified_at"),
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
            
            return VerifyTokenResponse(user=user, tenant=tenant, onboarding=onboarding)
            
    except (ValidationError, AuthenticationError, HTTPException):
        raise
    except Exception as e:
        logger.error(f"❌ Token verification handler error: {e}", exc_info=True)
        raise AuthenticationError("Token verification failed")


def _schedule_registration_notification(request: Request) -> None:
    notification = getattr(request.state, "registration_notification", None)
    if not notification:
        return
    request.state.registration_notification = None
    from app.services.leads_service import notify_self_service_registration

    asyncio.create_task(notify_self_service_registration(**notification))


def _registration_attribution_from(notification) -> Optional[RegistrationAttribution]:
    if not notification:
        return None
    values = {
        key: notification.get(key)
        for key in ("source", "content", "campaign", "variant")
        if notification.get(key)
    }
    return RegistrationAttribution(**values) if values else None


def _registration_attribution(request: Request) -> Optional[RegistrationAttribution]:
    stored = getattr(request.state, "registration_attribution", None)
    if stored:
        return stored
    return _registration_attribution_from(
        getattr(request.state, "registration_notification", None)
    )


async def verify_registration_token(
    request: Request,
    response: Response,
    token: str,
) -> VerifyTokenResponse:
    """Consume a registration-only opaque token and create/resume pending access."""
    try:
        tenant_context = require_valid_tenant(request)
        async with get_db_connection() as conn:
            registration = await _complete_registration_login(
                conn,
                request=request,
                response=response,
                tenant_context=tenant_context,
                email=None,
                credential=token,
                kind="token",
                login_method="onboarding_magic_link",
                opaque_token=True,
            )
            if not registration:
                raise AuthenticationError("Invalid or expired registration token")
            user, tenant, onboarding = registration
            result = VerifyTokenResponse(
                user=user,
                tenant=tenant,
                onboarding=onboarding,
                registration_attribution=_registration_attribution(request),
            )
        _schedule_registration_notification(request)
        return result
    except (ValidationError, AuthenticationError, HTTPException):
        raise
    except Exception as exc:
        logger.error("Registration token verification failed", exc_info=True)
        raise AuthenticationError("Registration verification failed") from exc


async def verify_registration_code(
    request: Request,
    response: Response,
    email: str,
    code: str,
) -> VerifyCodeResponse:
    """Consume a registration-only code while keeping email in the body."""
    try:
        email = normalize_email(email)
        tenant_context = require_valid_tenant(request)
        async with get_db_connection() as conn:
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
                raise AuthenticationError("Invalid or expired registration code")
            user, tenant, onboarding = registration
            result = VerifyCodeResponse(
                user=user,
                tenant=tenant,
                onboarding=onboarding,
                registration_attribution=_registration_attribution(request),
            )
        _schedule_registration_notification(request)
        return result
    except (ValidationError, AuthenticationError, HTTPException):
        raise
    except Exception as exc:
        logger.error("Registration code verification failed", exc_info=True)
        raise AuthenticationError("Registration verification failed") from exc

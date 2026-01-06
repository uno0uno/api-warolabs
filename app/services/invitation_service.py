import logging
import secrets
from datetime import datetime, timedelta
from typing import Optional, List
from uuid import uuid4
from fastapi import Request, Response
from app.database import get_db_connection
from app.core.security import set_session_cookie, get_client_ip, get_current_user_id
from app.core.middleware import require_valid_tenant, require_valid_session
from app.core.exceptions import AuthenticationError, ValidationError, AuthorizationError
from app.models.invitation import (
    SendInvitationRequest,
    SendInvitationResponse,
    AcceptInvitationResponse,
    PendingInvitationsResponse,
    CancelInvitationResponse,
    InvitationData,
    InvitationUser
)

logger = logging.getLogger(__name__)

INVITATION_EXPIRY_DAYS = 7
DEFAULT_NATIONALITY_ID = 170  # Colombia


async def send_invitation(request: Request, payload: SendInvitationRequest) -> SendInvitationResponse:
    """
    Send team invitation email
    Creates profile if user doesn't exist, then creates invitation
    """
    try:
        # Get session context (this has the ACTUAL tenant the user is working with)
        session_context = require_valid_session(request)
        current_user_id = session_context.user_id
        session_tenant_id = session_context.tenant_id

        if not current_user_id:
            raise AuthenticationError("Authentication required")

        if not session_tenant_id:
            raise ValidationError("No tenant selected in session")

        # Get tenant context from site for email/branding info
        tenant_context = require_valid_tenant(request)

        logger.info(f"📨 Invitation request for {payload.email} from user {current_user_id}")
        logger.info(f"🏷️ Session Tenant ID: {session_tenant_id}")

        async with get_db_connection() as conn:
            # Get tenant info from database using session tenant_id
            tenant_info = await conn.fetchrow(
                """SELECT t.id, t.name, t.slug, t.email as tenant_email, ts.brand_name
                   FROM tenants t
                   LEFT JOIN tenant_sites ts ON ts.tenant_id = t.id
                   WHERE t.id = $1
                   LIMIT 1""",
                session_tenant_id
            )

            if not tenant_info:
                raise ValidationError("Tenant not found")

            tenant_name = tenant_info['name']
            # Use site's verified email for sending, fall back to tenant email
            tenant_email = tenant_context.tenant_email or tenant_info['tenant_email']
            brand_name = tenant_info['brand_name'] or tenant_name

            logger.info(f"🏷️ Tenant: {tenant_name} (ID: {session_tenant_id})")

            # Check if current user has permission (admin or superuser)
            permission_query = """
                SELECT role FROM tenant_members
                WHERE user_id = $1 AND tenant_id = $2
            """
            user_role = await conn.fetchval(permission_query, current_user_id, session_tenant_id)

            if user_role not in ('admin', 'superuser'):
                raise AuthorizationError("Only admin or superuser can send invitations")

            # Check if email already exists in profile
            existing_user = await conn.fetchrow(
                "SELECT id, email FROM profile WHERE email = $1",
                payload.email
            )

            if existing_user:
                # Check if already member of this tenant
                existing_member = await conn.fetchval(
                    "SELECT id FROM tenant_members WHERE user_id = $1 AND tenant_id = $2",
                    existing_user['id'], session_tenant_id
                )
                if existing_member:
                    raise ValidationError("User is already a member of this team")

                user_id = existing_user['id']
                logger.info(f"✅ Existing user found: {user_id}")
            else:
                # Create new profile
                create_profile_query = """
                    INSERT INTO profile (
                        id, email, name, phone_number, nationality_id, created_at, updated_at
                    )
                    VALUES (gen_random_uuid(), $1, $2, $3, $4, NOW(), NOW())
                    RETURNING id
                """
                user_id = await conn.fetchval(
                    create_profile_query,
                    payload.email,
                    payload.name,
                    payload.phone,
                    DEFAULT_NATIONALITY_ID
                )
                logger.info(f"🆕 New profile created: {user_id}")

            # Check for existing pending invitation
            existing_invitation = await conn.fetchval(
                """SELECT id FROM tenant_invitations
                   WHERE user_id = $1 AND tenant_id = $2 AND status = 'pending'""",
                user_id, session_tenant_id
            )
            if existing_invitation:
                # Cancel old invitation
                await conn.execute(
                    "UPDATE tenant_invitations SET status = 'cancelled' WHERE id = $1",
                    existing_invitation
                )
                logger.info(f"🔄 Cancelled old pending invitation: {existing_invitation}")

            # Generate secure token
            token = secrets.token_hex(32)
            expires_at = datetime.utcnow() + timedelta(days=INVITATION_EXPIRY_DAYS)
            invitation_id = uuid4()

            # Create invitation
            insert_invitation_query = """
                INSERT INTO tenant_invitations (
                    id, tenant_id, user_id, email, token, role,
                    invited_by, expires_at, status
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'pending')
                RETURNING id, email, role, status, expires_at
            """
            invitation = await conn.fetchrow(
                insert_invitation_query,
                invitation_id,
                session_tenant_id,
                user_id,
                payload.email,
                token,
                payload.role.value,
                current_user_id,
                expires_at
            )
            logger.info(f"✉️ Invitation created: {invitation_id}")

            # Send invitation email
            from app.services.aws_ses_service import ses_service
            from app.templates.invitation_template import get_invitation_template, get_invitation_subject
            from app.config import settings

            # Generate invitation URL
            if settings.is_development:
                origin = request.headers.get('origin', 'http://localhost:8080')
                base_url = origin
            else:
                base_url = f"https://{tenant_context.site}"

            invitation_url = f"{base_url}/auth/accept-invitation?token={token}"

            # Get inviter name
            inviter_name = await conn.fetchval(
                "SELECT name FROM profile WHERE id = $1",
                current_user_id
            )

            # Prepare template context
            template_context = {
                'brand_name': brand_name,
                'tenant_name': tenant_name,
                'inviter_name': inviter_name or 'Un administrador',
                'invitee_name': payload.name,
                'role': 'Administrador' if payload.role.value == 'admin' else 'Super Usuario',
            }

            # Generate email content
            html_template = get_invitation_template(invitation_url, template_context)
            subject = get_invitation_subject(brand_name)

            from_name = f"{inviter_name or 'Equipo'} - {brand_name}"

            # Send email
            email_sent = await ses_service.send_email(
                from_email=tenant_email,
                from_name=from_name,
                to_emails=[payload.email],
                subject=subject,
                html_body=html_template
            )

            if email_sent:
                logger.info(f"✅ Invitation email sent to {payload.email}")
            else:
                logger.error(f"❌ Failed to send invitation email to {payload.email}")
                logger.info(f"🔗 FALLBACK: Invitation URL: {invitation_url}")

            return SendInvitationResponse(
                success=True,
                message="Invitación enviada exitosamente",
                data=InvitationData(
                    id=invitation['id'],
                    email=invitation['email'],
                    name=payload.name,
                    role=invitation['role'],
                    status=invitation['status'],
                    expiresAt=invitation['expires_at']
                )
            )

    except (ValidationError, AuthenticationError, AuthorizationError):
        raise
    except Exception as e:
        logger.error(f"❌ Send invitation error: {e}", exc_info=True)
        raise ValidationError("Failed to send invitation")


async def accept_invitation(request: Request, response: Response, token: str) -> AcceptInvitationResponse:
    """
    Accept team invitation and create session
    """
    try:
        tenant_context = require_valid_tenant(request)

        logger.info(f"🎟️ Accepting invitation with token: {token[:16]}...")

        async with get_db_connection() as conn:
            # Find valid invitation
            invitation_query = """
                SELECT ti.*, p.email, p.name, p.id as user_id
                FROM tenant_invitations ti
                JOIN profile p ON ti.user_id = p.id
                WHERE ti.token = $1
                AND ti.status = 'pending'
                AND ti.expires_at > NOW()
            """
            invitation = await conn.fetchrow(invitation_query, token)

            if not invitation:
                raise AuthenticationError("Invitación inválida o expirada")

            logger.info(f"✅ Valid invitation found for user: {invitation['user_id']}")

            # Mark invitation as accepted
            await conn.execute(
                """UPDATE tenant_invitations
                   SET status = 'accepted', accepted_at = NOW()
                   WHERE id = $1""",
                invitation['id']
            )

            # Add user to tenant_members
            member_exists = await conn.fetchval(
                "SELECT id FROM tenant_members WHERE user_id = $1 AND tenant_id = $2",
                invitation['user_id'], invitation['tenant_id']
            )

            if not member_exists:
                await conn.execute(
                    """INSERT INTO tenant_members (id, user_id, tenant_id, role)
                       VALUES (gen_random_uuid(), $1, $2, $3)""",
                    invitation['user_id'],
                    invitation['tenant_id'],
                    invitation['role']
                )
                logger.info(f"👥 User added to tenant_members with role: {invitation['role']}")
            else:
                # Update role if already exists
                await conn.execute(
                    "UPDATE tenant_members SET role = $1 WHERE user_id = $2 AND tenant_id = $3",
                    invitation['role'],
                    invitation['user_id'],
                    invitation['tenant_id']
                )
                logger.info(f"🔄 Updated existing member role to: {invitation['role']}")

            # Create session (same as magic link flow)
            session_id = secrets.token_hex(16)
            expires_at = datetime.utcnow() + timedelta(days=7)
            client_ip = get_client_ip(request)
            user_agent = request.headers.get('user-agent')

            session_query = """
                INSERT INTO sessions (
                    id, user_id, tenant_id, expires_at,
                    created_at, last_activity_at,
                    ip_address, user_agent, login_method, is_active
                )
                VALUES ($1, $2, $3, $4, NOW(), NOW(), $5, $6, 'invitation', true)
            """
            await conn.execute(
                session_query,
                session_id,
                invitation['user_id'],
                invitation['tenant_id'],
                expires_at,
                client_ip,
                user_agent
            )
            logger.info(f"🎫 Session created: {session_id}")

            # Set session cookie
            await set_session_cookie(response, session_id, tenant_context.site)

            # Get tenant name for notification
            tenant_name = await conn.fetchval(
                "SELECT name FROM tenants WHERE id = $1",
                invitation['tenant_id']
            )

            # Send Discord notification
            try:
                from app.services.discord_service import discord_session_service
                if discord_session_service:
                    await discord_session_service.notify_new_session(
                        user_email=invitation['email'],
                        user_name=invitation['name'],
                        tenant_name=tenant_name,
                        login_method="invitation",
                        ip_address=client_ip,
                        user_agent=user_agent
                    )
            except Exception as e:
                logger.error(f"Failed to send Discord notification: {e}")

            return AcceptInvitationResponse(
                success=True,
                message="¡Bienvenido al equipo!",
                user=InvitationUser(
                    id=invitation['user_id'],
                    email=invitation['email'],
                    name=invitation['name']
                )
            )

    except AuthenticationError:
        raise
    except Exception as e:
        logger.error(f"❌ Accept invitation error: {e}", exc_info=True)
        raise AuthenticationError("Failed to accept invitation")


async def get_pending_invitations(request: Request) -> PendingInvitationsResponse:
    """
    Get pending invitations for current tenant
    """
    try:
        tenant_context = require_valid_tenant(request)
        current_user_id = await get_current_user_id(request)

        if not current_user_id:
            raise AuthenticationError("Authentication required")

        async with get_db_connection() as conn:
            # Check permission
            user_role = await conn.fetchval(
                "SELECT role FROM tenant_members WHERE user_id = $1 AND tenant_id = $2",
                current_user_id, tenant_context.tenant_id
            )

            if user_role not in ('admin', 'superuser'):
                raise AuthorizationError("Only admin or superuser can view invitations")

            # Get pending invitations
            query = """
                SELECT
                    ti.id, ti.email, ti.role, ti.status, ti.expires_at,
                    p.name as invitee_name,
                    inviter.name as invited_by_name
                FROM tenant_invitations ti
                LEFT JOIN profile p ON ti.user_id = p.id
                LEFT JOIN profile inviter ON ti.invited_by = inviter.id
                WHERE ti.tenant_id = $1 AND ti.status = 'pending'
                ORDER BY ti.expires_at DESC
            """
            rows = await conn.fetch(query, tenant_context.tenant_id)

            invitations = [
                InvitationData(
                    id=row['id'],
                    email=row['email'],
                    name=row['invitee_name'],
                    role=row['role'],
                    status=row['status'],
                    expiresAt=row['expires_at'],
                    invitedByName=row['invited_by_name']
                )
                for row in rows
            ]

            return PendingInvitationsResponse(success=True, data=invitations)

    except (AuthenticationError, AuthorizationError):
        raise
    except Exception as e:
        logger.error(f"❌ Get pending invitations error: {e}", exc_info=True)
        raise ValidationError("Failed to get invitations")


async def cancel_invitation(request: Request, invitation_id: str) -> CancelInvitationResponse:
    """
    Cancel a pending invitation
    """
    try:
        # Use session tenant, not site tenant
        session_context = require_valid_session(request)
        current_user_id = session_context.user_id
        session_tenant_id = session_context.tenant_id

        if not current_user_id:
            raise AuthenticationError("Authentication required")

        if not session_tenant_id:
            raise ValidationError("No tenant selected in session")

        async with get_db_connection() as conn:
            # Check permission
            user_role = await conn.fetchval(
                "SELECT role FROM tenant_members WHERE user_id = $1 AND tenant_id = $2",
                current_user_id, session_tenant_id
            )

            if user_role not in ('admin', 'superuser'):
                raise AuthorizationError("Only admin or superuser can cancel invitations")

            # Cancel invitation
            result = await conn.execute(
                """UPDATE tenant_invitations
                   SET status = 'cancelled'
                   WHERE id = $1 AND tenant_id = $2 AND status = 'pending'""",
                invitation_id, session_tenant_id
            )

            if result == "UPDATE 0":
                raise ValidationError("Invitation not found or already processed")

            logger.info(f"🚫 Invitation cancelled: {invitation_id}")

            return CancelInvitationResponse(
                success=True,
                message="Invitación cancelada exitosamente"
            )

    except (AuthenticationError, AuthorizationError, ValidationError):
        raise
    except Exception as e:
        logger.error(f"❌ Cancel invitation error: {e}", exc_info=True)
        raise ValidationError("Failed to cancel invitation")

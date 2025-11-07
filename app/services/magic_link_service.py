import logging
import secrets
import random
from datetime import datetime, timedelta
from typing import Optional
from uuid import uuid4
from fastapi import Request, Response
from app.database import get_db_connection
from app.core.security import set_session_cookie, get_client_ip
from app.core.middleware import require_valid_tenant
from app.core.exceptions import AuthenticationError, ValidationError
from app.models.auth import User, Tenant, MagicLinkResponse, VerifyCodeResponse, VerifyTokenResponse

logger = logging.getLogger(__name__)

async def send_magic_link(request: Request, email: str, redirect: Optional[str] = None) -> MagicLinkResponse:
    """
    Send magic link using tenant context from middleware
    """
    try:
        # Get validated tenant context from middleware
        tenant_context = require_valid_tenant(request)
        
        logger.info(f"📧 Magic link request for {email} from {tenant_context.site}")
        logger.info(f"🏷️ Using tenant: {tenant_context.tenant_name} (ID: {tenant_context.tenant_id})")
        
        async with get_db_connection() as conn:
            # Generate secure token and verification code
            token = secrets.token_hex(32)
            verification_code = str(random.randint(100000, 999999))  # 6-digit code
            expires_at = datetime.utcnow() + timedelta(minutes=15)  # 15 minutes
            
            # Check if user exists, if not create one
            user_query = 'SELECT * FROM profile WHERE email = $1 LIMIT 1'
            user_result = await conn.fetchrow(user_query, email)
            
            if not user_result:
                logger.info(f"👤 Creating new user for email: {email}")
                insert_user_query = """
                    INSERT INTO profile (email, name, nationality_id, phone_number) 
                    VALUES ($1, $2, $3, $4) 
                    RETURNING id
                """
                user_result = await conn.fetchrow(insert_user_query, 
                    email, 
                    email.split('@')[0], 
                    1,  # default nationality_id 
                    '+1234567890'  # default phone_number
                )
                user_id = user_result['id']
                logger.info(f"✅ User created with ID: {user_id}")
            else:
                user_id = user_result['id']
                logger.info(f"👤 User found with ID: {user_id}")
            
            # Mark old unused magic tokens as expired for this user
            await conn.execute(
                'UPDATE magic_tokens SET used = true, used_at = NOW() WHERE user_id = $1 AND used = false', 
                user_id
            )
            
            # Save new magic token to database with tenant_id from context
            insert_token_query = """
                INSERT INTO magic_tokens (user_id, token, verification_code, expires_at, tenant_id, used, created_at, used_at) 
                VALUES ($1, $2, $3, $4, $5, false, NOW(), NULL)
            """
            await conn.execute(insert_token_query, 
                user_id, token, verification_code, expires_at, tenant_context.tenant_id
            )
            logger.info(f"🔑 Magic token saved for user: {user_id}, tenant: {tenant_context.tenant_id}")
            
            # Send magic link email using AWS SES
            from app.services.aws_ses_service import ses_service
            from app.templates.magic_link_template import get_magic_link_template, get_magic_link_subject
            from app.config import settings
            
            # Generate magic link URL based on detected tenant site
            if settings.is_development:
                # In development, redirect to frontend (warocol.com runs on port 8080)
                base_url = "http://localhost:8080"
            else:
                # In production, use the detected tenant site from middleware
                base_url = f"https://{tenant_context.site}"
            
            magic_link_url = f"{base_url}/auth/verify?token={token}&email={email}"
            if redirect:
                magic_link_url += f"&redirect={redirect}"
            
            # Prepare tenant context for template
            template_context = {
                'brand_name': tenant_context.brand_name,
                'tenant_name': tenant_context.tenant_name,
                'admin_name': 'Saifer 101 (Anderson Arévalo)',  # Default admin
                'admin_email': tenant_context.tenant_email,
            }
            
            # Generate email content
            html_template = get_magic_link_template(magic_link_url, verification_code, template_context)
            subject = get_magic_link_subject(tenant_context.brand_name)
            
            # Determine sender name with enterprise branding
            from_name = f"Saifer 101 (Anderson Arévalo) - {tenant_context.brand_name}"
            
            # Send email via AWS SES
            email_sent = await ses_service.send_email(
                from_email=tenant_context.tenant_email,
                from_name=from_name,
                to_emails=[email],
                subject=subject,
                html_body=html_template
            )
            
            if email_sent:
                logger.info(f"✅ Magic link email sent to {email} from {tenant_context.tenant_email}")
                logger.info(f"🔗 Magic link URL: {magic_link_url}")
            else:
                logger.error(f"❌ Failed to send magic link email to {email}")
                # In case of email failure, still log the code for development
                logger.info(f"🔢 FALLBACK: Verification code for {email}: {verification_code}")
            
            logger.info(f"📧 Email sender: {from_name}")
            logger.info(f"🏢 Brand: {tenant_context.brand_name}")
            
            return MagicLinkResponse()
            
    except ValidationError:
        raise
    except Exception as e:
        logger.error(f"❌ Magic link handler error: {e}", exc_info=True)
        raise ValidationError("Failed to send magic link")


async def verify_code(request: Request, response: Response, email: str, code: str) -> VerifyCodeResponse:
    """
    Verify magic link code using tenant context from middleware
    """
    try:
        # Get validated tenant context from middleware
        tenant_context = require_valid_tenant(request)
        
        logger.info(f"🔢 Verification request for {email} from {tenant_context.site}")
        
        async with get_db_connection() as conn:
            # Verify code and get user info - must match the tenant from context
            verify_query = """
                SELECT mt.*, p.email, p.name, p.id as user_id, p.created_at as user_created_at,
                       tm.role as user_role
                FROM magic_tokens mt
                JOIN profile p ON mt.user_id = p.id
                LEFT JOIN tenant_members tm ON tm.user_id = p.id AND tm.tenant_id = mt.tenant_id
                WHERE p.email = $1 AND mt.verification_code = $2 
                AND mt.tenant_id = $3
                AND mt.expires_at > NOW() AND mt.used = false
                LIMIT 1
            """
            
            token_data = await conn.fetchrow(verify_query, email, code, tenant_context.tenant_id)
            
            if not token_data:
                logger.warning(f"❌ Invalid verification code for {email} on {tenant_context.site}")
                raise AuthenticationError("Invalid or expired verification code")
            
            logger.info(f"✅ Valid verification code for user: {token_data['user_id']}")
            
            # Mark token as used
            await conn.execute(
                'UPDATE magic_tokens SET used = true, used_at = NOW() WHERE verification_code = $1 AND user_id = $2',
                code, token_data['user_id']
            )
            logger.info("✅ Verification code marked as used")
            
            # End all previous active sessions for this user to prevent duplicate cookies
            await conn.execute(
                'UPDATE sessions SET is_active = false, ended_at = NOW(), end_reason = $1 WHERE user_id = $2 AND is_active = true',
                'new_login', token_data['user_id']
            )
            logger.info(f"🧹 Ended all previous active sessions for user: {token_data['user_id']}")
            
            # Create session with tenant context
            session_id = secrets.token_hex(16)
            expires_at = datetime.utcnow() + timedelta(days=30)  # 30 days
            
            # Get client info for analytics
            client_ip = get_client_ip(request)
            user_agent = request.headers.get('user-agent')
            
            session_query = """
                INSERT INTO sessions (
                  id, user_id, tenant_id, expires_at, 
                  created_at, last_activity_at, 
                  ip_address, user_agent, login_method, is_active
                )
                VALUES ($1, $2, $3, $4, NOW(), NOW(), $5, $6, 'verification_code', true)
                RETURNING id
            """
            await conn.execute(session_query, 
                session_id, token_data['user_id'], tenant_context.tenant_id, 
                expires_at, client_ip, user_agent
            )
            logger.info(f"🎫 Session created: {session_id} for {tenant_context.tenant_name}")
            
            # Set session cookie with correct domain for tenant
            set_session_cookie(response, session_id, tenant_context.site)
            
            # Build response models
            user = User(
                id=token_data['user_id'],
                email=token_data['email'],
                name=token_data['name'],
                createdAt=token_data['user_created_at'] or datetime.utcnow()
            )
            
            tenant = Tenant(
                id=tenant_context.tenant_id,
                name=tenant_context.tenant_name,
                slug=tenant_context.tenant_slug
            )
            
            logger.info(f"✅ Verification successful for {email} on {tenant_context.site}")
            
            return VerifyCodeResponse(user=user, tenant=tenant)
            
    except (ValidationError, AuthenticationError):
        raise
    except Exception as e:
        logger.error(f"❌ Verification code handler error: {e}", exc_info=True)
        raise AuthenticationError("Verification failed")


async def verify_token(request: Request, response: Response, email: str, token: str) -> VerifyTokenResponse:
    """
    Verify magic link token using tenant context from middleware
    """
    try:
        # Get validated tenant context from middleware
        tenant_context = require_valid_tenant(request)
        
        logger.info(f"🔍 Token verification request for {email} from {tenant_context.site}")
        
        async with get_db_connection() as conn:
            # Find valid unused magic token with tenant context
            verify_query = """
                SELECT mt.*, p.email, p.name, p.id as user_id, p.created_at as user_created_at,
                       tm.role as user_role
                FROM magic_tokens mt
                JOIN profile p ON mt.user_id = p.id
                LEFT JOIN tenant_members tm ON tm.user_id = p.id AND tm.tenant_id = mt.tenant_id
                WHERE p.email = $1 AND mt.token = $2 
                AND mt.tenant_id = $3
                AND mt.expires_at > NOW() AND mt.used = false
                LIMIT 1
            """
            
            token_data = await conn.fetchrow(verify_query, email, token, tenant_context.tenant_id)
            
            if not token_data:
                logger.warning(f"❌ Invalid or expired token for {email} on {tenant_context.site}")
                raise AuthenticationError("Invalid or expired token")
            
            logger.info(f"✅ Valid token found for user: {token_data['user_id']}")
            
            # Mark token as used
            await conn.execute(
                'UPDATE magic_tokens SET used = true, used_at = NOW() WHERE token = $1 AND user_id = $2',
                token, token_data['user_id']
            )
            logger.info("✅ Token marked as used")
            
            # End all previous active sessions for this user to prevent duplicate cookies
            await conn.execute(
                'UPDATE sessions SET is_active = false, ended_at = NOW(), end_reason = $1 WHERE user_id = $2 AND is_active = true',
                'new_login', token_data['user_id']
            )
            logger.info(f"🧹 Ended all previous active sessions for user: {token_data['user_id']}")
            
            # Create session with tenant context
            session_id = secrets.token_hex(16)
            expires_at = datetime.utcnow() + timedelta(days=30)  # 30 days
            
            # Get client info for analytics
            client_ip = get_client_ip(request)
            user_agent = request.headers.get('user-agent')
            
            session_query = """
                INSERT INTO sessions (
                  id, user_id, tenant_id, expires_at, 
                  created_at, last_activity_at, 
                  ip_address, user_agent, login_method, is_active
                )
                VALUES ($1, $2, $3, $4, NOW(), NOW(), $5, $6, 'magic_link', true)
                RETURNING id
            """
            await conn.execute(session_query, 
                session_id, token_data['user_id'], tenant_context.tenant_id, 
                expires_at, client_ip, user_agent
            )
            logger.info(f"🎫 Session created: {session_id} for {tenant_context.tenant_name}")
            
            # Set session cookie with correct domain for tenant
            set_session_cookie(response, session_id, tenant_context.site)
            
            # Build response model
            user = User(
                id=token_data['user_id'],
                email=token_data['email'],
                name=token_data['name'],
                createdAt=token_data['user_created_at'] or datetime.utcnow()
            )
            
            logger.info(f"✅ Token verification successful for {email} on {tenant_context.site}")
            
            return VerifyTokenResponse(user=user)
            
    except (ValidationError, AuthenticationError):
        raise
    except Exception as e:
        logger.error(f"❌ Token verification handler error: {e}", exc_info=True)
        raise AuthenticationError("Token verification failed")
"""
OTP Service
Handles email-based OTP verification for online ordering (NO authentication required)
"""
import random
import string
from typing import Optional
from uuid import UUID
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
from app.database import get_db_connection
from app.services.aws_ses_service import AWSSESService
from app.services.email_sender import resolve_sender_email_value
from app.core.exceptions import APIError
from app.core.email_utils import normalize_email
import logging

logger = logging.getLogger(__name__)

# Configuration
OTP_LENGTH = 6
OTP_EXPIRY_MINUTES = 5
OTP_MAX_ATTEMPTS = 3
OTP_RESEND_COOLDOWN_SECONDS = 60


def generate_otp_code() -> str:
    """Generate random 6-digit OTP code"""
    return ''.join(random.choices(string.digits, k=OTP_LENGTH))


async def send_otp_email(
    email: str,
    cart_id: Optional[UUID] = None,
) -> dict:
    """
    Send OTP code via email for cart verification (PUBLIC)

    Returns:
        - success: bool
        - expires_in: int (seconds)
        - message: str
    """
    try:
        email = normalize_email(email)
        async with get_db_connection() as conn:
            # Check if there's a recent non-expired OTP
            recent_otp_query = """
                SELECT id, otp_code, expires_at, created_at
                FROM otp_verifications
                WHERE lower(trim(email)) = $1
                AND cart_id IS NOT DISTINCT FROM $2
                AND is_verified = false
                AND expires_at > now()
                ORDER BY created_at DESC
                LIMIT 1
            """
            recent_otp = await conn.fetchrow(recent_otp_query, email, cart_id)

            # Check cooldown (60 seconds between resends)
            if recent_otp:
                time_since_last = datetime.now(timezone.utc) - recent_otp['created_at']
                if time_since_last.total_seconds() < OTP_RESEND_COOLDOWN_SECONDS:
                    cooldown_remaining = OTP_RESEND_COOLDOWN_SECONDS - int(time_since_last.total_seconds())
                    raise APIError(
                        f"Por favor espera {cooldown_remaining} segundos antes de reenviar el código",
                        status_code=429
                    )

            # Generate new OTP
            otp_code = generate_otp_code()
            expires_at = datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRY_MINUTES)

            # Save to database
            insert_query = """
                INSERT INTO otp_verifications (
                    email, otp_code, cart_id, expires_at, max_attempts
                )
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
            """
            await conn.fetchrow(
                insert_query,
                email,
                otp_code,
                cart_id,
                expires_at,
                OTP_MAX_ATTEMPTS
            )

            # Send email via AWS SES
            ses_service = AWSSESService()

            subject = "Código de verificación - WARO Colombia"
            text_body = f"""
Hola,

Tu código de verificación para completar tu pedido es:

{otp_code}

Este código es válido por {OTP_EXPIRY_MINUTES} minutos.

Si no solicitaste este código, puedes ignorar este mensaje.

Gracias,
WARO Colombia
            """.strip()

            email_sent = await ses_service.send_email(
                from_email=resolve_sender_email_value(),
                from_name="WARO Colombia",
                to_emails=[email],
                subject=subject,
                html_body=None,  # Plain text to avoid spam filters
                text_body=text_body
            )

            if not email_sent:
                raise APIError("Error al enviar el correo. Intenta de nuevo.", status_code=500)

            logger.info(f"OTP sent to {email} for cart {cart_id}")

            return {
                "success": True,
                "expires_in": OTP_EXPIRY_MINUTES * 60,  # seconds
                "message": f"Código enviado a {email}. Válido por {OTP_EXPIRY_MINUTES} minutos."
            }

    except APIError:
        raise
    except Exception as e:
        logger.error(f"Error sending OTP: {str(e)}")
        raise APIError(f"Error al enviar OTP: {str(e)}", status_code=500)


async def verify_otp_code(
    email: str,
    cart_id: Optional[UUID],
    otp_code: str,
    phone_number: Optional[str] = None,
) -> dict:
    """
    Verify OTP code for cart (PUBLIC)

    Returns:
        - success: bool
        - customer_id: UUID (if verified)
        - pickup_pin: str (if order_type=pickup)
        - is_verified: bool
    """
    try:
        email = normalize_email(email)
        async with get_db_connection() as conn:
            async with conn.transaction():
                # Get latest OTP for this email + cart
                otp_query = """
                    SELECT id, otp_code, is_verified, attempts, max_attempts, expires_at
                    FROM otp_verifications
                    WHERE lower(trim(email)) = $1
                    AND cart_id IS NOT DISTINCT FROM $2
                    ORDER BY created_at DESC
                    LIMIT 1
                """
                otp_row = await conn.fetchrow(otp_query, email, cart_id)

                if not otp_row:
                    raise HTTPException(
                        status_code=404,
                        detail="No se encontró código de verificación. Solicita uno nuevo."
                    )

                # Check if already verified
                if otp_row['is_verified']:
                    raise HTTPException(
                        status_code=400,
                        detail="Este código ya fue verificado."
                    )

                # Check if expired
                if otp_row['expires_at'] < datetime.now(timezone.utc):
                    raise HTTPException(
                        status_code=400,
                        detail="Código expirado. Solicita uno nuevo."
                    )

                # Check max attempts
                if otp_row['attempts'] >= otp_row['max_attempts']:
                    raise HTTPException(
                        status_code=400,
                        detail="Máximo de intentos alcanzado. Solicita un nuevo código."
                    )

                # Increment attempts
                await conn.execute(
                    "UPDATE otp_verifications SET attempts = attempts + 1 WHERE id = $1",
                    otp_row['id']
                )

                # Verify code
                if otp_row['otp_code'] != otp_code:
                    remaining_attempts = otp_row['max_attempts'] - (otp_row['attempts'] + 1)
                    if remaining_attempts > 0:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Código incorrecto. Te quedan {remaining_attempts} intentos."
                        )
                    else:
                        raise HTTPException(
                            status_code=400,
                            detail="Código incorrecto. Máximo de intentos alcanzado."
                        )

                # Mark as verified
                await conn.execute(
                    "UPDATE otp_verifications SET is_verified = true, verified_at = now() WHERE id = $1",
                    otp_row['id']
                )

                # Get or create customer
                customer_id = await get_or_create_customer(conn, email, phone_number)

                pickup_pin = None
                if cart_id is not None:
                    # Update cart with verification
                    cart_update_query = """
                        UPDATE online_carts
                        SET is_verified = true,
                            verified_email = $1,
                            customer_id = $2,
                            updated_at = now()
                        WHERE id = $3
                        RETURNING id, order_type
                    """
                    cart_row = await conn.fetchrow(cart_update_query, email, customer_id, cart_id)

                    if not cart_row:
                        raise HTTPException(status_code=404, detail="Carrito no encontrado")

                    # Generate pickup PIN if order_type = pickup
                    if cart_row['order_type'] == 'pickup':
                        pickup_pin = generate_pickup_pin()
                        await conn.execute(
                            "UPDATE online_carts SET pickup_pin = $1, pin_generated_at = now() WHERE id = $2",
                            pickup_pin,
                            cart_id
                        )

                logger.info(f"OTP verified for email {email}, cart {cart_id}")

                return {
                    "success": True,
                    "customer_id": str(customer_id),
                    "is_verified": True,
                    "pickup_pin": pickup_pin,
                    "message": "Verificación exitosa"
                }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error verifying OTP: {str(e)}")
        raise APIError(f"Error al verificar OTP: {str(e)}", status_code=500)


def normalize_phone_number(phone_number: Optional[str]) -> Optional[str]:
    """Normalize phone format used by customer search/create flows."""
    if phone_number is None:
        return None

    normalized = phone_number.strip().replace(' ', '').replace('-', '')
    return normalized or None


def _profile_value_missing(value) -> bool:
    return value is None or str(value).strip() == ""


async def get_or_create_customer(
    conn,
    email: str,
    phone_number: Optional[str] = None,
) -> UUID:
    """
    Search for an existing customer by email/phone or create a new one.
    Returns customer_id (UUID)
    """
    email = normalize_email(email)
    phone_number = normalize_phone_number(phone_number)

    email_query = """
        SELECT id, email, phone_number FROM profile
        WHERE lower(trim(email)) = $1
        LIMIT 1
    """
    email_row = await conn.fetchrow(email_query, email)

    if phone_number is None:
        if email_row:
            return email_row['id']

        create_customer_query = """
            INSERT INTO profile (email, phone_number)
            VALUES ($1, '')
            RETURNING id
        """
        new_customer = await conn.fetchrow(create_customer_query, email)

        logger.info(f"Created new customer with email {email}")
        return new_customer['id']

    phone_query = """
        SELECT id, email, phone_number FROM profile
        WHERE phone_number = $1
        LIMIT 1
    """
    phone_row = await conn.fetchrow(phone_query, phone_number)

    if email_row and phone_row and email_row['id'] != phone_row['id']:
        raise HTTPException(
            status_code=409,
            detail="El correo y el teléfono pertenecen a clientes distintos."
        )

    if email_row:
        existing_phone = normalize_phone_number(email_row['phone_number'])
        if existing_phone is None:
            await conn.execute(
                """
                UPDATE profile
                SET phone_number = $2, updated_at = now()
                WHERE id = $1
                """,
                email_row['id'],
                phone_number,
            )
            return email_row['id']

        if existing_phone == phone_number:
            return email_row['id']

        raise HTTPException(
            status_code=409,
            detail="El correo ya está asociado a otro teléfono."
        )

    if phone_row:
        existing_email = phone_row['email']
        if _profile_value_missing(existing_email):
            await conn.execute(
                """
                UPDATE profile
                SET email = $2, updated_at = now()
                WHERE id = $1
                """,
                phone_row['id'],
                email,
            )
            return phone_row['id']

        if normalize_email(existing_email) == email:
            return phone_row['id']

        raise HTTPException(
            status_code=409,
            detail="El teléfono ya está asociado a otro correo."
        )

    create_customer_query = """
        INSERT INTO profile (email, phone_number)
        VALUES ($1, $2)
        RETURNING id
    """
    new_customer = await conn.fetchrow(create_customer_query, email, phone_number)

    logger.info(f"Created new customer with email {email} and phone {phone_number}")
    return new_customer['id']


def generate_pickup_pin() -> str:
    """Generate random 6-digit pickup PIN"""
    return ''.join(random.choices(string.digits, k=6))


async def validate_customer(
    phone_number: str,
    cart_total: float,
    tenant_id: Optional[UUID] = None,
    cart_id: Optional[UUID] = None,
) -> dict:
    """
    Validate customer eligibility to order (PUBLIC)
    Checks:
    - Blacklist
    - Customer tier (new/intermediate/trusted)
    - Spending limits for new customers

    Returns:
        - can_order: bool
        - is_blacklisted: bool
        - customer_tier: str
        - max_amount: float (if limited)
        - warnings: list of strings
    """
    try:
        async with get_db_connection() as conn:
            warnings = []
            tenant_limit = await get_tenant_online_order_limit(conn, tenant_id, cart_id)

            # 1. Check blacklist
            blacklist_query = """
                SELECT reason, expires_at
                FROM customer_blacklist
                WHERE phone_number = $1
                AND (expires_at IS NULL OR expires_at > now())
                LIMIT 1
            """
            blacklist_row = await conn.fetchrow(blacklist_query, phone_number)

            if blacklist_row:
                if blacklist_row['expires_at'] is None:
                    reason = "Este número está bloqueado permanentemente."
                else:
                    expires_str = blacklist_row['expires_at'].strftime('%Y-%m-%d')
                    reason = f"Este número está bloqueado temporalmente hasta {expires_str}."

                return {
                    "can_order": False,
                    "is_blacklisted": True,
                    "customer_tier": None,
                    "reason": reason,
                    "warnings": []
                }

            # 2. Find customer by phone
            customer_query = """
                SELECT id FROM profile
                WHERE phone_number = $1
                LIMIT 1
            """
            customer_row = await conn.fetchrow(customer_query, phone_number)

            if not customer_row:
                # New customer - apply strict limits
                max_amount = resolve_customer_max_amount("new", tenant_limit)
                if max_amount and cart_total > max_amount:
                    return {
                        "can_order": False,
                        "is_blacklisted": False,
                        "customer_tier": "new",
                        "max_amount": max_amount,
                        "reason": f"El primer pedido no puede superar ${max_amount:,.0f} COP",
                        "warnings": []
                    }

                warnings.append("Primer pedido - verificación requerida")
                return {
                    "can_order": True,
                    "is_blacklisted": False,
                    "customer_tier": "new",
                    "max_amount": max_amount,
                    "warnings": warnings
                }

            # 3. Calculate customer tier based on completed orders
            customer_id = customer_row['id']
            tier_query = """
                SELECT COUNT(*) as order_count
                FROM orders
                WHERE customer_id = $1
                AND status = 'completed'
            """
            tier_row = await conn.fetchrow(tier_query, customer_id)
            order_count = tier_row['order_count'] if tier_row else 0

            if order_count == 0:
                tier = "new"
            elif order_count < 3:
                tier = "intermediate"
            else:
                tier = "trusted"

            max_amount = resolve_customer_max_amount(tier, tenant_limit)

            # Check limit
            if max_amount and cart_total > max_amount:
                return {
                    "can_order": False,
                    "is_blacklisted": False,
                    "customer_tier": tier,
                    "max_amount": max_amount,
                    "reason": f"Tu límite actual es ${max_amount:,.0f} COP. Completa más pedidos para aumentarlo.",
                    "warnings": warnings
                }

            # Check recent failures
            failure_query = """
                SELECT COUNT(*) as failure_count
                FROM order_failures
                WHERE phone_number = $1
                AND failure_date > now() - interval '30 days'
            """
            failure_row = await conn.fetchrow(failure_query, phone_number)
            failure_count = failure_row['failure_count'] if failure_row else 0

            if failure_count >= 1:
                warnings.append(f"Tienes {failure_count} pedido(s) no recogido(s) en el último mes")

            return {
                "can_order": True,
                "is_blacklisted": False,
                "customer_tier": tier,
                "max_amount": max_amount,
                "warnings": warnings
            }

    except Exception as e:
        logger.error(f"Error validating customer: {str(e)}")
        raise APIError(f"Error al validar cliente: {str(e)}", status_code=500)


async def get_tenant_online_order_limit(conn, tenant_id: Optional[UUID], cart_id: Optional[UUID]) -> Optional[float]:
    """
    Return tenant override for online order amount validation.

    NULL means use the existing tier defaults. 0 means no amount limit.
    """
    resolved_tenant_id = tenant_id
    if not resolved_tenant_id and cart_id:
        resolved_tenant_id = await conn.fetchval(
            "SELECT tenant_id FROM online_carts WHERE id = $1",
            cart_id,
        )

    if not resolved_tenant_id:
        return None

    value = await conn.fetchval(
        "SELECT online_order_max_amount FROM tenant_public_profiles WHERE tenant_id = $1",
        resolved_tenant_id,
    )
    if value is None:
        return None

    return float(value)


def resolve_customer_max_amount(tier: str, tenant_limit: Optional[float]) -> Optional[float]:
    if tenant_limit is not None:
        return None if tenant_limit <= 0 else tenant_limit

    if tier == "new":
        return 50000
    if tier == "intermediate":
        return 100000
    return None

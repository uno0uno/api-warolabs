"""
Leads service - public lead capture from homepage CTA buttons
"""
import asyncio
import logging
import json
from typing import Optional

from app.core.email_utils import normalize_email
from app.services.email_sender import resolve_sender_email_value

logger = logging.getLogger(__name__)


_FOOTER = (
    "----\n"
    "Saifer 101 (Anderson Arévalo)\n"
    "Fundador WaRo Colombia\n"
    "Bogotá, D.C, Colombia\n"
    "Tel: 3142047013\n"
    "Correo: anderson.arevalo@warolabs.com\n"
    "Tecnología colombiana para el mundo. warocol.com"
)


async def notify_self_service_registration(
    *,
    email: str,
    phone: Optional[str],
    phone_country_code: Optional[int],
    tenant_name: Optional[str],
    status: Optional[str],
    source: Optional[str],
    content: Optional[str],
    campaign: Optional[str],
    variant: Optional[str],
) -> None:
    """Notify the internal lead channel without sending legacy advisory email."""
    try:
        from app.services.discord_service import discord_leads_service

        if not discord_leads_service:
            return
        sent = await discord_leads_service.notify_new_lead(
            email=email,
            phone=phone,
            phone_country_code=phone_country_code,
            button_source="self_service_registration",
            tenant_name=tenant_name,
            status=status,
            source=source,
            content=content,
            campaign=campaign,
            variant=variant,
        )
        if not sent:
            logger.error("[self_service_registration] Discord notification was not delivered")
    except Exception:
        logger.exception("[self_service_registration] Discord notification failed")


def _build_confirmation_email(email: str) -> str:
    return (
        "WARO Colombia\n\n"
        "Hola,\n\n"
        "Gracias por comunicarte con nosotros. Hemos recibido tu mensaje y nos pondremos en contacto contigo muy pronto.\n\n"
        "Si tienes alguna pregunta urgente, no dudes en responder a este correo.\n\n"
        "¡Hasta pronto!\n"
        "El equipo de WARO Colombia\n\n"
        f"{_FOOTER}\n\n"
        f"Este correo fue enviado a {email} porque completaste un formulario en warocol.com"
    )


def _build_duplicate_email(email: str) -> str:
    return (
        "WARO Colombia\n\n"
        "Hola,\n\n"
        "Disculpa si enviaste el formulario más de una vez. Ya tenemos tu solicitud registrada y "
        "nos pondremos en contacto contigo pronto.\n\n"
        "No necesitas hacer nada más, ya estás en nuestra lista.\n\n"
        "¡Hasta pronto!\n"
        "El equipo de WARO Colombia\n\n"
        f"{_FOOTER}\n\n"
        f"Este correo fue enviado a {email} porque completaste un formulario en warocol.com"
    )


async def capture_lead(
    conn,
    email: str,
    phone: str,
    ip_address: Optional[str],
    user_agent: Optional[str],
    button_source: str,
) -> dict:
    """
    Capture a lead from the homepage.

    Flow:
    1. UPSERT profile (email is UNIQUE) — nationality_id=0 placeholder, phone_country_code=57
    2. INSERT lead linked to profile (only if not already a lead for this profile)
    3. INSERT lead_interaction to track the CTA click event
    """

    email = normalize_email(email)

    # 1. UPSERT profile
    profile = await conn.fetchrow(
        """
        INSERT INTO profile (email, phone_number, phone_country_code, nationality_id)
        VALUES ($1, $2, 57, 0)
        ON CONFLICT (email) DO UPDATE
            SET phone_number = EXCLUDED.phone_number
        RETURNING id
        """,
        email,
        phone,
    )
    profile_id = profile["id"]
    logger.info(f"📥 [capture_lead] Profile upserted: {profile_id}")

    # 2. Check if lead already exists for this profile (only homepage_cta counts as duplicate;
    #    an access_request lead means the user is filling the full form for the first time)
    existing_lead = await conn.fetchrow(
        "SELECT id FROM leads WHERE profile_id = $1 AND source = 'homepage_cta' ORDER BY created_at ASC LIMIT 1",
        profile_id,
    )
    is_duplicate = existing_lead is not None

    if is_duplicate:
        lead_id = existing_lead["id"]
    else:
        new_lead = await conn.fetchrow(
            """
            INSERT INTO leads (profile_id, email, source, status)
            VALUES ($1, $2, 'homepage_cta', 'active')
            RETURNING id
            """,
            profile_id,
            email,
        )
        lead_id = new_lead["id"]
    logger.info(f"📥 [capture_lead] Lead id: {lead_id} (duplicate={is_duplicate})")

    # 3. INSERT lead_interaction — type differs for duplicate vs new
    interaction_type = "duplicate_submit" if is_duplicate else "homepage_cta"
    metadata = json.dumps({"button": button_source, "duplicate": is_duplicate})
    await conn.execute(
        """
        INSERT INTO lead_interactions
            (lead_id, interaction_type, source, ip_address, user_agent, metadata)
        VALUES ($1, $2, 'homepage', $3, $4, $5::jsonb)
        """,
        lead_id,
        interaction_type,
        ip_address,
        user_agent,
        metadata,
    )
    logger.info(f"📥 [capture_lead] Interaction '{interaction_type}' recorded for lead {lead_id}")

    # Fire-and-forget: Discord notification + email (non-blocking, always fires)
    asyncio.create_task(_send_notifications(email, phone, button_source, ip_address, is_duplicate))

    return {"profile_id": str(profile_id), "lead_id": str(lead_id), "is_duplicate": is_duplicate}


async def capture_access_request(
    conn,
    email: str,
    phone: Optional[str],
    ip_address: Optional[str],
    user_agent: Optional[str],
    button_source: str = "access_request",
) -> dict:
    """
    Capture an access request from the login page.

    Flow:
    1. UPSERT profile by email (update phone if provided)
    2. INSERT lead with source='access_request' (skip if already exists)
    3. INSERT lead_interaction to track the event
    """

    email = normalize_email(email)

    # 1. UPSERT profile — update phone if provided, otherwise leave existing value
    if phone:
        await conn.execute(
            """
            INSERT INTO profile (email, phone_number, phone_country_code, nationality_id)
            VALUES ($1, $2, 57, 0)
            ON CONFLICT (email) DO UPDATE
                SET phone_number = EXCLUDED.phone_number
            """,
            email,
            phone,
        )
    else:
        await conn.execute(
            """
            INSERT INTO profile (email, phone_number, phone_country_code, nationality_id)
            VALUES ($1, '', 57, 0)
            ON CONFLICT (email) DO NOTHING
            """,
            email,
        )
    profile = await conn.fetchrow(
        "SELECT id FROM profile WHERE lower(trim(email)) = $1",
        email,
    )
    profile_id = profile["id"]
    logger.info(f"📥 [capture_access_request] Profile id: {profile_id}")

    # 2. INSERT lead (skip if one already exists for this profile)
    lead = await conn.fetchrow(
        """
        INSERT INTO leads (profile_id, email, source, status)
        VALUES ($1, $2, 'access_request', 'active')
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        profile_id,
        email,
    )

    if lead is None:
        lead = await conn.fetchrow(
            "SELECT id FROM leads WHERE profile_id = $1 ORDER BY created_at ASC LIMIT 1",
            profile_id,
        )

    lead_id = lead["id"]
    logger.info(f"📥 [capture_access_request] Lead id: {lead_id}")

    # 3. INSERT lead_interaction for every access request
    import json as _json
    metadata = _json.dumps({"button": button_source})
    await conn.execute(
        """
        INSERT INTO lead_interactions
            (lead_id, interaction_type, source, ip_address, user_agent, metadata)
        VALUES ($1, 'access_request', 'login_page', $2, $3, $4::jsonb)
        """,
        lead_id,
        ip_address,
        user_agent,
        metadata,
    )
    logger.info(f"📥 [capture_access_request] Interaction recorded for lead {lead_id}")

    # Fire-and-forget notifications (non-blocking)
    asyncio.create_task(_send_access_request_notifications(email, phone, ip_address))

    return {"profile_id": str(profile_id), "lead_id": str(lead_id)}


async def _send_access_request_notifications(
    email: str,
    phone: Optional[str],
    ip_address: Optional[str],
) -> None:
    """Send Discord notification and confirmation email for access requests."""
    from app.services.discord_service import discord_leads_service
    from app.services.aws_ses_service import ses_service

    tasks = []

    if discord_leads_service:
        tasks.append(
            discord_leads_service.notify_new_lead(
                email=email,
                phone=phone,
                button_source="access_request",
                ip_address=ip_address,
            )
        )

    text_body = (
        "WARO Colombia\n\n"
        "Hola,\n\n"
        "Gracias por comunicarte con nosotros. Hemos recibido tu mensaje y nos pondremos en contacto contigo muy pronto.\n\n"
        "Si tienes alguna pregunta urgente, no dudes en responder a este correo.\n\n"
        "¡Hasta pronto!\n"
        "El equipo de WARO Colombia\n\n"
        f"Este correo fue enviado a {email} porque completaste un formulario en warocol.com"
    )

    tasks.append(
        ses_service.send_email(
            from_email=resolve_sender_email_value(),
            from_name="WARO Colombia",
            to_emails=[email],
            subject="¡Gracias por contactarnos! — WARO Colombia",
            text_body=text_body,
        )
    )

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            logger.error(f"[capture_access_request] Notification error: {result}")


async def _send_notifications(
    email: str,
    phone: str,
    button_source: str,
    ip_address: Optional[str],
    is_duplicate: bool = False,
) -> None:
    """Send Discord notification and confirmation email without blocking the response."""
    from app.services.discord_service import discord_leads_service
    from app.services.aws_ses_service import ses_service

    tasks = []

    if discord_leads_service:
        if is_duplicate:
            tasks.append(
                discord_leads_service.send_notification(
                    title="🔁 Reintento de Lead",
                    description=(
                        f"**Email:** {email}\n"
                        f"**Teléfono:** +57 {phone}\n"
                        f"**Botón:** {button_source}\n"
                        f"**IP:** {ip_address or '-'}\n"
                        "_Este email ya estaba registrado como lead._"
                    ),
                    color=16776960,  # Yellow
                )
            )
        else:
            tasks.append(
                discord_leads_service.notify_new_lead(
                    email=email,
                    phone=phone,
                    button_source=button_source,
                    ip_address=ip_address,
                )
            )

    email_body = _build_duplicate_email(email) if is_duplicate else _build_confirmation_email(email)
    subject = (
        "Ya tenemos tu solicitud — WARO Colombia"
        if is_duplicate
        else "¡Gracias por contactarnos! — WARO Colombia"
    )
    tasks.append(
        ses_service.send_email(
            from_email=resolve_sender_email_value(),
            from_name="WARO Colombia",
            to_emails=[email],
            subject=subject,
            text_body=email_body,
        )
    )

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            logger.error(f"[capture_lead] Notification error: {result}")

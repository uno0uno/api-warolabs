"""
Leads service - public lead capture from homepage CTA buttons
"""
import asyncio
import logging
import json
from typing import Optional

logger = logging.getLogger(__name__)


def _build_confirmation_email(email: str) -> str:
    return f"""
<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>¡Gracias por contactarnos!</title>
</head>
<body style="margin:0;padding:0;background:#F8F9FA;font-family:Lato,Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#F8F9FA;padding:40px 0;">
    <tr>
      <td align="center">
        <table width="600" cellpadding="0" cellspacing="0" style="background:#FFFFFF;border-radius:12px;overflow:hidden;max-width:600px;width:100%;">
          <!-- Header -->
          <tr>
            <td style="background:#7C3AED;padding:32px 40px;text-align:center;">
              <h1 style="margin:0;color:#FFFFFF;font-size:24px;font-weight:700;letter-spacing:-0.3px;">WARO Colombia</h1>
            </td>
          </tr>
          <!-- Body -->
          <tr>
            <td style="padding:40px;">
              <p style="margin:0 0 16px;font-size:16px;line-height:1.6;color:#1F1D35;">Hola,</p>
              <p style="margin:0 0 16px;font-size:16px;line-height:1.6;color:#1F1D35;">
                Gracias por comunicarte con nosotros. Hemos recibido tu mensaje y nos pondremos en contacto contigo muy pronto.
              </p>
              <p style="margin:0 0 32px;font-size:16px;line-height:1.6;color:#1F1D35;">
                Si tienes alguna pregunta urgente, no dudes en responder a este correo.
              </p>
              <p style="margin:0;font-size:16px;line-height:1.6;color:#1F1D35;">
                ¡Hasta pronto!<br />
                <strong>El equipo de WARO Colombia</strong>
              </p>
            </td>
          </tr>
          <!-- Footer -->
          <tr>
            <td style="background:#F0F1F3;padding:20px 40px;text-align:center;">
              <p style="margin:0;font-size:13px;color:#4B5565;">
                Este correo fue enviado a {email} porque completaste un formulario en warocol.com
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""


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

    # 2. INSERT lead (skip if one already exists for this profile)
    lead = await conn.fetchrow(
        """
        INSERT INTO leads (profile_id, email, source, status)
        VALUES ($1, $2, 'homepage_cta', 'active')
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        profile_id,
        email,
    )

    if lead is None:
        # Lead already existed — fetch it
        lead = await conn.fetchrow(
            "SELECT id FROM leads WHERE profile_id = $1 ORDER BY created_at ASC LIMIT 1",
            profile_id,
        )

    lead_id = lead["id"]
    logger.info(f"📥 [capture_lead] Lead id: {lead_id}")

    # 3. INSERT lead_interaction for every CTA click
    metadata = json.dumps({"button": button_source})
    await conn.execute(
        """
        INSERT INTO lead_interactions
            (lead_id, interaction_type, source, ip_address, user_agent, metadata)
        VALUES ($1, 'homepage_cta', 'homepage', $2, $3, $4::jsonb)
        """,
        lead_id,
        ip_address,
        user_agent,
        metadata,
    )
    logger.info(f"📥 [capture_lead] Interaction recorded for lead {lead_id} via '{button_source}'")

    # Fire-and-forget: Discord notification + confirmation email (non-blocking)
    asyncio.create_task(_send_notifications(email, phone, button_source, ip_address))

    return {"profile_id": str(profile_id), "lead_id": str(lead_id)}


async def capture_access_request(
    conn,
    email: str,
    ip_address: Optional[str],
    user_agent: Optional[str],
    button_source: str = "access_request",
) -> dict:
    """
    Capture an access request from the login page.

    Flow:
    1. UPSERT profile by email only (no phone touch)
    2. INSERT lead with source='access_request' (skip if already exists)
    3. INSERT lead_interaction to track the event
    """

    # 1. UPSERT profile by email — do not overwrite phone if profile exists
    await conn.execute(
        """
        INSERT INTO profile (email, nationality_id)
        VALUES ($1, 0)
        ON CONFLICT (email) DO NOTHING
        """,
        email,
    )
    profile = await conn.fetchrow(
        "SELECT id FROM profile WHERE email = $1",
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
    asyncio.create_task(_send_access_request_notifications(email, ip_address))

    return {"profile_id": str(profile_id), "lead_id": str(lead_id)}


async def _send_access_request_notifications(
    email: str,
    ip_address: Optional[str],
) -> None:
    """Send Discord notification and confirmation email for access requests."""
    from app.services.discord_service import discord_leads_service
    from app.services.aws_ses_service import ses_service
    from app.config import settings

    tasks = []

    if discord_leads_service:
        tasks.append(
            discord_leads_service.notify_new_lead(
                email=email,
                button_source="access_request",
                ip_address=ip_address,
            )
        )

    tasks.append(
        ses_service.send_email(
            from_email=settings.email_from,
            from_name="WARO Colombia",
            to_emails=[email],
            subject="¡Gracias por contactarnos! — WARO Colombia",
            html_body=_build_confirmation_email(email),
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
) -> None:
    """Send Discord notification and confirmation email without blocking the response."""
    from app.services.discord_service import discord_leads_service
    from app.services.aws_ses_service import ses_service
    from app.config import settings

    tasks = []

    if discord_leads_service:
        tasks.append(
            discord_leads_service.notify_new_lead(
                email=email,
                phone=phone,
                button_source=button_source,
                ip_address=ip_address,
            )
        )

    tasks.append(
        ses_service.send_email(
            from_email=settings.email_from,
            from_name="WARO Colombia",
            to_emails=[email],
            subject="¡Gracias por contactarnos! — WARO Colombia",
            html_body=_build_confirmation_email(email),
        )
    )

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            logger.error(f"[capture_lead] Notification error: {result}")

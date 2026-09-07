"""
Leads service - public lead capture from homepage CTA buttons
"""
import asyncio
import logging
import json
from typing import Optional
from urllib.parse import urlparse
from uuid import UUID

from app.core.email_utils import normalize_email
from app.services.email_sender import resolve_sender_email_value

logger = logging.getLogger(__name__)

# WARO Colombia owner profile — campaigns for other brands share this DB.
WAROCOL_CAMPAIGN_PROFILE_ID = UUID("7fe92b2c-d99e-4c70-b0cb-74af6326da5a")

_PUBLIC_CAMPAIGN_SQL = """
SELECT c.id, c.slug, c.name, landing.content AS landing_content
FROM campaign c
LEFT JOIN LATERAL (
  SELECT tv.content
  FROM campaign_template_versions ctv
  JOIN template_versions tv ON tv.id = ctv.template_version_id
  JOIN templates t ON t.id = tv.template_id
  WHERE ctv.campaign_id = c.id
    AND coalesce(ctv.is_active, true) = true
    AND t.template_type = 'landing'
    AND coalesce(t.is_deleted, false) = false
  ORDER BY tv.created_at DESC NULLS LAST, tv.version_number DESC
  LIMIT 1
) landing ON true
WHERE lower(c.slug) = lower($1)
  AND c.profile_id = $2
  AND coalesce(c.is_deleted, false) = false
  AND lower(c.status) = 'active'
  AND (c.start_date IS NULL OR c.start_date <= now())
  AND (c.end_date IS NULL OR c.end_date >= now())
LIMIT 1
"""

_BLOCKED_MEDIA_HOSTS = frozenset({
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
    "www.youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
})


class PublicCampaignNotFound(Exception):
    """No active WARO Colombia campaign for this public slug."""


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


def _blank_to_none(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _optional_text(value) -> Optional[str]:
    if not isinstance(value, str):
        return None
    return _blank_to_none(value)


def _parse_landing_content(raw) -> dict:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray, memoryview)):
        raw = bytes(raw).decode("utf-8")
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _safe_public_media_url(value: Optional[str]) -> Optional[str]:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        return None
    if host in _BLOCKED_MEDIA_HOSTS or host.endswith(".youtube.com"):
        return None
    return text


def _extract_image_url(parsed: dict) -> Optional[str]:
    image_url = parsed.get("image_url")
    if isinstance(image_url, str):
        return _blank_to_none(image_url)
    image = parsed.get("image")
    if isinstance(image, dict):
        content = image.get("content")
        if isinstance(content, str):
            return _blank_to_none(content)
    if isinstance(image, str):
        return _blank_to_none(image)
    return None


def _public_campaign_from_row(row) -> dict:
    name = row["name"]
    raw = row["landing_content"] if "landing_content" in row else None
    parsed = _parse_landing_content(raw)
    return {
        "id": row["id"],
        "slug": row["slug"],
        "name": name,
        "title": _optional_text(parsed.get("title")) or name,
        "description": _optional_text(parsed.get("description")),
        "cta_label": _optional_text(parsed.get("cta_label")),
        "microcopy": _optional_text(parsed.get("microcopy")),
        "image_url": _safe_public_media_url(_extract_image_url(parsed)),
        "video_url": _safe_public_media_url(_optional_text(parsed.get("video_url"))),
    }


async def get_public_campaign(conn, slug: str) -> Optional[dict]:
    cleaned = (slug or "").strip()
    if not cleaned:
        return None
    row = await conn.fetchrow(
        _PUBLIC_CAMPAIGN_SQL,
        cleaned,
        WAROCOL_CAMPAIGN_PROFILE_ID,
    )
    if row is None:
        return None
    return _public_campaign_from_row(row)


async def capture_lead(
    conn,
    email: str,
    phone: str,
    ip_address: Optional[str],
    user_agent: Optional[str],
    button_source: str,
    visitor_key: Optional[str] = None,
    campaign_slug: Optional[str] = None,
    utm_source: Optional[str] = None,
    utm_medium: Optional[str] = None,
    utm_campaign: Optional[str] = None,
    utm_term: Optional[str] = None,
    utm_content: Optional[str] = None,
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

    campaign = None
    campaign_slug = _blank_to_none(campaign_slug)
    if campaign_slug:
        campaign = await get_public_campaign(conn, campaign_slug)
        if campaign is None:
            raise PublicCampaignNotFound(campaign_slug)

    utm_source = _blank_to_none(utm_source)
    utm_medium = _blank_to_none(utm_medium)
    utm_campaign = _blank_to_none(utm_campaign)
    utm_term = _blank_to_none(utm_term)
    utm_content = _blank_to_none(utm_content)
    lead_source = "landing" if campaign else "homepage_cta"

    # 2. Check if lead already exists for this profile (homepage_cta and landing share
    #    one row; an access_request-only profile still gets a new capture row)
    existing_lead = await conn.fetchrow(
        """
        SELECT id FROM leads
        WHERE profile_id = $1 AND source IN ('homepage_cta', 'landing')
        ORDER BY created_at ASC
        LIMIT 1
        """,
        profile_id,
    )
    is_duplicate = existing_lead is not None

    if is_duplicate:
        lead_id = existing_lead["id"]
        if any((utm_source, utm_medium, utm_campaign, utm_term, utm_content)):
            await conn.execute(
                """
                UPDATE leads SET
                    utm_source = coalesce(utm_source, $2),
                    utm_medium = coalesce(utm_medium, $3),
                    utm_campaign = coalesce(utm_campaign, $4),
                    utm_term = coalesce(utm_term, $5),
                    utm_content = coalesce(utm_content, $6)
                WHERE id = $1
                """,
                lead_id,
                utm_source,
                utm_medium,
                utm_campaign,
                utm_term,
                utm_content,
            )
    else:
        new_lead = await conn.fetchrow(
            """
            INSERT INTO leads (
                profile_id, email, source, status,
                utm_source, utm_medium, utm_campaign, utm_term, utm_content
            )
            VALUES ($1, $2, $3, 'active', $4, $5, $6, $7, $8)
            RETURNING id
            """,
            profile_id,
            email,
            lead_source,
            utm_source,
            utm_medium,
            utm_campaign,
            utm_term,
            utm_content,
        )
        lead_id = new_lead["id"]
    logger.info(f"📥 [capture_lead] Lead id: {lead_id} (duplicate={is_duplicate})")

    # 3. INSERT lead_interaction — type differs for duplicate vs new
    interaction_type = "duplicate_submit" if is_duplicate else lead_source
    interaction_source = "landing" if campaign else "homepage"
    metadata = json.dumps({"button": button_source, "duplicate": is_duplicate})
    stored_visitor_key = (visitor_key or "").strip() or None
    campaign_id = campaign["id"] if campaign else None
    await conn.execute(
        """
        INSERT INTO lead_interactions
            (lead_id, interaction_type, source, ip_address, user_agent, metadata,
             visitor_key, campaign_id, medium, campaign, term, content)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10, $11, $12)
        """,
        lead_id,
        interaction_type,
        interaction_source,
        ip_address,
        user_agent,
        metadata,
        stored_visitor_key,
        campaign_id,
        utm_medium,
        utm_campaign,
        utm_term,
        utm_content,
    )
    logger.info(f"📥 [capture_lead] Interaction '{interaction_type}' recorded for lead {lead_id}")

    if campaign_id is not None:
        await conn.execute(
            """
            INSERT INTO campaign_leads (campaign_id, lead_id)
            VALUES ($1, $2)
            ON CONFLICT (campaign_id, lead_id) DO NOTHING
            """,
            campaign_id,
            lead_id,
        )

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
    visitor_key: Optional[str] = None,
    campaign_slug: Optional[str] = None,
    utm_source: Optional[str] = None,
    utm_medium: Optional[str] = None,
    utm_campaign: Optional[str] = None,
    utm_term: Optional[str] = None,
    utm_content: Optional[str] = None,
) -> dict:
    """
    Capture an access request from the login page.

    Flow:
    1. UPSERT profile by email (update phone if provided)
    2. INSERT lead with source='access_request' (skip if already exists)
    3. INSERT lead_interaction to track the event
    """

    email = normalize_email(email)

    utm_source = _blank_to_none(utm_source)
    utm_medium = _blank_to_none(utm_medium)
    utm_campaign = _blank_to_none(utm_campaign)
    utm_term = _blank_to_none(utm_term)
    utm_content = _blank_to_none(utm_content)

    campaign = None
    campaign_slug = _blank_to_none(campaign_slug)
    if campaign_slug:
        campaign = await get_public_campaign(conn, campaign_slug)
        if campaign is None:
            raise PublicCampaignNotFound(campaign_slug)

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
    metadata = _json.dumps({"button": button_source, "campaign_slug": campaign_slug, "utm": {
        "source": utm_source, "medium": utm_medium, "campaign": utm_campaign, "term": utm_term, "content": utm_content,
    } if any((utm_source, utm_medium, utm_campaign, utm_term, utm_content)) else None})
    stored_visitor_key = (visitor_key or "").strip() or None
    campaign_id = campaign["id"] if campaign else None
    interaction_source = "landing" if campaign else "login_page"
    # Also persist UTM to leads row when first seen (best-effort, coalesce)
    if any((utm_source, utm_medium, utm_campaign, utm_term, utm_content)):
        await conn.execute(
            """
            UPDATE leads SET
                utm_source = coalesce(utm_source, $2),
                utm_medium = coalesce(utm_medium, $3),
                utm_campaign = coalesce(utm_campaign, $4),
                utm_term = coalesce(utm_term, $5),
                utm_content = coalesce(utm_content, $6)
            WHERE id = $1
            """,
            lead_id,
            utm_source,
            utm_medium,
            utm_campaign,
            utm_term,
            utm_content,
        )
    await conn.execute(
        """
        INSERT INTO lead_interactions
            (lead_id, interaction_type, source, ip_address, user_agent, metadata,
             visitor_key, campaign_id, medium, campaign, term, content)
        VALUES ($1, 'access_request', $2, $3, $4, $5::jsonb, $6, $7, $8, $9, $10, $11)
        """,
        lead_id,
        interaction_source,
        ip_address,
        user_agent,
        metadata,
        stored_visitor_key,
        campaign_id,
        utm_medium,
        utm_campaign,
        utm_term,
        utm_content,
    )
    logger.info(f"📥 [capture_access_request] Interaction recorded for lead {lead_id}")
    if campaign_id is not None:
        await conn.execute(
            """
            INSERT INTO campaign_leads (campaign_id, lead_id)
            VALUES ($1, $2)
            ON CONFLICT (campaign_id, lead_id) DO NOTHING
            """,
            campaign_id,
            lead_id,
        )

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

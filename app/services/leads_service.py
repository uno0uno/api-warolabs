"""
Leads service - public lead capture from homepage CTA buttons
"""
import logging
import json

logger = logging.getLogger(__name__)


async def capture_lead(
    conn,
    email: str,
    phone: str,
    ip_address: str | None,
    user_agent: str | None,
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

    return {"profile_id": str(profile_id), "lead_id": str(lead_id)}

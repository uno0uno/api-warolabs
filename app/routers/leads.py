"""
Leads router - public endpoints for lead capture
No authentication required
"""
import logging
import re
from typing import Optional
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field, field_validator
from app.database import get_db_connection
from app.services import leads_service
from app.core.email_utils import normalize_email

logger = logging.getLogger(__name__)

router = APIRouter()


def _optional_utm(value: object) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text[:100] if text else None
    return None


def _optional_visitor_key(value: object) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


class AccessRequestBody(BaseModel):
    email: EmailStr
    phone: Optional[str] = None
    button_source: str = "access_request"
    visitor_key: Optional[str] = Field(default=None, max_length=128)
    campaign_slug: Optional[str] = Field(default=None, max_length=255)
    utm_source: Optional[str] = Field(default=None, max_length=100)
    utm_medium: Optional[str] = Field(default=None, max_length=100)
    utm_campaign: Optional[str] = Field(default=None, max_length=100)
    utm_term: Optional[str] = Field(default=None, max_length=100)
    utm_content: Optional[str] = Field(default=None, max_length=100)

    @field_validator("email", mode="before")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        return normalize_email(v)

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        digits = re.sub(r"\D", "", v)
        if len(digits) < 7 or len(digits) > 10:
            raise ValueError("El teléfono debe tener entre 7 y 10 dígitos")
        return digits

    @field_validator("visitor_key", "campaign_slug", mode="before")
    @classmethod
    def _strip_visitor_key(cls, v: object) -> Optional[str]:
        return _optional_visitor_key(v)

    @field_validator(
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        mode="before",
    )
    @classmethod
    def _strip_utm(cls, v: object) -> Optional[str]:
        return _optional_utm(v)


class LeadCaptureRequest(BaseModel):
    email: EmailStr
    phone: str
    button_source: str = "comenzar"
    visitor_key: Optional[str] = Field(default=None, max_length=128)
    campaign_slug: Optional[str] = Field(default=None, max_length=255)
    utm_source: Optional[str] = Field(default=None, max_length=100)
    utm_medium: Optional[str] = Field(default=None, max_length=100)
    utm_campaign: Optional[str] = Field(default=None, max_length=100)
    utm_term: Optional[str] = Field(default=None, max_length=100)
    utm_content: Optional[str] = Field(default=None, max_length=100)

    @field_validator("email", mode="before")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        return normalize_email(v)

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        digits = re.sub(r"\D", "", v)
        if len(digits) < 7 or len(digits) > 10:
            raise ValueError("El teléfono debe tener entre 7 y 10 dígitos")
        return digits

    @field_validator("visitor_key", "campaign_slug", mode="before")
    @classmethod
    def _strip_visitor_key(cls, v: object) -> Optional[str]:
        return _optional_visitor_key(v)

    @field_validator(
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        mode="before",
    )
    @classmethod
    def _strip_utm(cls, v: object) -> Optional[str]:
        return _optional_utm(v)


@router.post("/capture")
async def capture_lead(body: LeadCaptureRequest, request: Request):
    """
    Capture a homepage lead (PUBLIC - no auth required).

    Creates or updates a profile, creates a lead record, and logs the CTA interaction.

    Body:
    - email: required
    - phone: required (digits only, 7-10 chars, Colombia +57 applied on frontend)
    - button_source: 'comenzar' | 'habla_con_nosotros'
    """
    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    logger.info(f"📥 [leads/capture] email={body.email} source={body.button_source} ip={ip_address}")

    try:
        async with get_db_connection() as conn:
            result = await leads_service.capture_lead(
                conn=conn,
                email=str(body.email),
                phone=body.phone,
                ip_address=ip_address,
                user_agent=user_agent,
                button_source=body.button_source,
                visitor_key=body.visitor_key,
                campaign_slug=body.campaign_slug,
                utm_source=body.utm_source,
                utm_medium=body.utm_medium,
                utm_campaign=body.utm_campaign,
                utm_term=body.utm_term,
                utm_content=body.utm_content,
            )
    except leads_service.PublicCampaignNotFound:
        raise HTTPException(status_code=404, detail="Campaign not found")

    return {"success": True, "already_registered": result["is_duplicate"]}


@router.get("/campaigns/{slug}")
async def get_public_campaign(slug: str):
    """Public campaign payload for /landing/:slug (WARO Colombia only)."""
    async with get_db_connection() as conn:
        campaign = await leads_service.get_public_campaign(conn, slug)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return {
        "slug": campaign["slug"],
        "name": campaign["name"],
        "title": campaign["title"],
        "description": campaign["description"],
        "cta_label": campaign["cta_label"],
        "microcopy": campaign["microcopy"],
        "image_url": campaign["image_url"],
        "video_url": campaign["video_url"],
    }


@router.post("/access-request")
async def capture_access_request(body: AccessRequestBody, request: Request):
    """
    Capture an access request from the login page (PUBLIC - no auth required).

    Called when a user tries to log in but has no account.
    Creates a lead and notifies the WARO team via Discord + SES.

    Body:
    - email: required (pre-filled from login form)
    - button_source: 'access_request' (default)
    """
    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    logger.info(f"📥 [leads/access-request] email={body.email} source={body.button_source} ip={ip_address}")

    try:
        async with get_db_connection() as conn:
            await leads_service.capture_access_request(
                conn=conn,
                email=str(body.email),
                phone=body.phone,
                ip_address=ip_address,
                user_agent=user_agent,
                button_source=body.button_source,
                visitor_key=body.visitor_key,
                campaign_slug=body.campaign_slug,
                utm_source=body.utm_source,
                utm_medium=body.utm_medium,
                utm_campaign=body.utm_campaign,
                utm_term=body.utm_term,
                utm_content=body.utm_content,
            )
    except leads_service.PublicCampaignNotFound:
        raise HTTPException(status_code=404, detail="Campaign not found")

    return {"success": True, "message": "¡Solicitud enviada! Nos pondremos en contacto contigo pronto."}

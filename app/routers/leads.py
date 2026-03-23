"""
Leads router - public endpoints for lead capture
No authentication required
"""
import logging
import re
from fastapi import APIRouter, Request
from pydantic import BaseModel, EmailStr, field_validator
from app.database import get_db_connection
from app.services import leads_service

logger = logging.getLogger(__name__)

router = APIRouter()


class AccessRequestBody(BaseModel):
    email: EmailStr
    button_source: str = "access_request"


class LeadCaptureRequest(BaseModel):
    email: EmailStr
    phone: str
    button_source: str = "comenzar"

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        digits = re.sub(r"\D", "", v)
        if len(digits) < 7 or len(digits) > 10:
            raise ValueError("El teléfono debe tener entre 7 y 10 dígitos")
        return digits


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

    async with get_db_connection() as conn:
        result = await leads_service.capture_lead(
            conn=conn,
            email=str(body.email),
            phone=body.phone,
            ip_address=ip_address,
            user_agent=user_agent,
            button_source=body.button_source,
        )

    return {
        "success": True,
        "already_registered": result["is_duplicate"],
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

    async with get_db_connection() as conn:
        await leads_service.capture_access_request(
            conn=conn,
            email=str(body.email),
            ip_address=ip_address,
            user_agent=user_agent,
            button_source=body.button_source,
        )

    return {"success": True, "message": "¡Solicitud enviada! Nos pondremos en contacto contigo pronto."}

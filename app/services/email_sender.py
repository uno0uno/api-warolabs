"""Shared sender resolution for SES transactional emails."""
from typing import Optional

from app.config import settings


DEFAULT_SENDER_EMAIL = "hola@warocol.com"


def resolve_sender_email_value(fallback_email: Optional[str] = None) -> str:
    email = (settings.email_from or fallback_email or DEFAULT_SENDER_EMAIL).strip()
    return email or DEFAULT_SENDER_EMAIL


async def resolve_sender_email_for_tenant(_tenant_id=None) -> str:
    return resolve_sender_email_value()

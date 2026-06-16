"""
Shared sender resolution for tenant-facing transactional emails.
"""
from typing import Optional, Union
from uuid import UUID

from app.config import settings
from app.database import get_db_connection


DEFAULT_SENDER_EMAIL = "hola@warocol.com"


def resolve_sender_email_value(sender_email: Optional[str] = None) -> str:
    email = (sender_email or settings.email_from or DEFAULT_SENDER_EMAIL).strip()
    return email or DEFAULT_SENDER_EMAIL


def resolve_sender_email_from_context(tenant_context) -> str:
    return resolve_sender_email_value(getattr(tenant_context, "tenant_email", None))


async def resolve_sender_email_for_tenant(tenant_id: Optional[Union[str, UUID]]) -> str:
    if not tenant_id:
        return resolve_sender_email_value()

    try:
        tenant_uuid = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))
        async with get_db_connection(use_transaction=False) as conn:
            tenant_email = await conn.fetchval(
                "SELECT email FROM tenants WHERE id = $1",
                tenant_uuid,
            )
        return resolve_sender_email_value(tenant_email)
    except Exception:
        return resolve_sender_email_value()

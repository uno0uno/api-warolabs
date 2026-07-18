"""
Invoice email delivery tracking (api-warolabs#657).

One row per send attempt. The pixel token is opaque (32 bytes urlsafe); only
its SHA-256 hex digest is persisted. No IP, no user-agent, no raw token.

Status semantics:
  pending — row created before SES call, not yet finalized
  sent    — SES accepted the request (NOT proof of delivery or human read)
  failed  — SES rejected the request
"""
import hashlib
import logging
import secrets
from typing import Any, Dict, List, Optional
from uuid import UUID

from app.config import settings
from app.database import get_db_connection

logger = logging.getLogger(__name__)

# 1x1 transparent GIF (43 bytes). Served identically for valid/invalid tokens.
PIXEL_GIF_BYTES = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\x00\x00\x00"
    b"!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00"
    b"\x02\x02D\x01\x00;"
)

PIXEL_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
    "Content-Type": "image/gif",
}


def generate_tracking_token() -> str:
    """Return a new opaque urlsafe token. Caller stores only the SHA-256."""
    return secrets.token_urlsafe(32)


def hash_tracking_token(token: str) -> str:
    """SHA-256 hex digest of the raw token. This is the only persisted form."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def build_pixel_url(token: str) -> str:
    """Public pixel URL embedded in the HTML body. Contains the RAW token."""
    base = settings.base_url.rstrip("/")
    return f"{base}/public/email-tracking/{token}.gif"


async def create_pending_delivery(
    *,
    tenant_id: UUID,
    order_id: UUID,
    recipient_email: str,
    tracking_token_hash: str,
) -> Optional[UUID]:
    """Insert a pending delivery row. Returns the delivery id.

    Fail-open: returns None when the table/DB is unavailable so the email
    flow keeps working exactly as before tracking existed (api-warolabs#657).
    """
    try:
        async with get_db_connection(use_transaction=False) as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO invoice_email_deliveries
                    (tenant_id, order_id, recipient_email, status, tracking_token_hash)
                VALUES ($1, $2, $3, 'pending', $4)
                RETURNING id
                """,
                tenant_id,
                order_id,
                recipient_email,
                tracking_token_hash,
            )
    except Exception as e:
        logger.error(f"invoice_email_deliveries: create_pending_delivery failed: {e}")
        return None
    return row["id"]


async def mark_delivery_sent(delivery_id: UUID) -> None:
    """Finalize as sent. Failure here must NOT trigger a resend; log + keep pending."""
    try:
        async with get_db_connection(use_transaction=False) as conn:
            await conn.execute(
                """
                UPDATE invoice_email_deliveries
                SET status = 'sent', sent_at = now(), updated_at = now()
                WHERE id = $1
                """,
                delivery_id,
            )
    except Exception as e:
        logger.error(
            f"invoice_email_deliveries: SES accepted but mark_sent failed for {delivery_id}: {e}"
        )


async def mark_delivery_failed(delivery_id: UUID, failure_code: Optional[str] = None) -> None:
    """Finalize as failed. failure_code is a short internal label, never user input."""
    try:
        async with get_db_connection(use_transaction=False) as conn:
            await conn.execute(
                """
                UPDATE invoice_email_deliveries
                SET status = 'failed', failed_at = now(), failure_code = $2, updated_at = now()
                WHERE id = $1
                """,
                delivery_id,
                (failure_code or "")[:120] or None,
            )
    except Exception as e:
        logger.error(
            f"invoice_email_deliveries: mark_failed failed for {delivery_id}: {e}"
        )


async def record_pixel_open(tracking_token_hash: str) -> None:
    """Atomically bump open_count and set first/last opened timestamps.

    Called by the public pixel endpoint. Unknown hashes are a silent no-op —
    the endpoint returns the identical pixel regardless.
    """
    try:
        async with get_db_connection(use_transaction=False) as conn:
            await conn.execute(
                """
                UPDATE invoice_email_deliveries
                SET open_count = open_count + 1,
                    first_opened_at = COALESCE(first_opened_at, now()),
                    last_opened_at = now(),
                    updated_at = now()
                WHERE tracking_token_hash = $1
                """,
                tracking_token_hash,
            )
    except Exception as e:
        logger.error(f"invoice_email_deliveries: record_pixel_open failed: {e}")


async def list_deliveries_for_order(
    *,
    tenant_id: UUID,
    order_id: UUID,
) -> List[Dict[str, Any]]:
    """Tenant-scoped history for one order, newest first. Never cross-tenant."""
    async with get_db_connection(use_transaction=False) as conn:
        rows = await conn.fetch(
            """
            SELECT id, recipient_email, status,
                   sent_at, failed_at, first_opened_at, last_opened_at,
                   open_count, failure_code, created_at
            FROM invoice_email_deliveries
            WHERE tenant_id = $1 AND order_id = $2
            ORDER BY created_at DESC
            LIMIT 50
            """,
            tenant_id,
            order_id,
        )
    return [dict(r) for r in rows]

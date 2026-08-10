"""
Colombia billing — Wompi transaction.updated handler (api-warolabs #353).

Deprecated for new billing activations (#798). Signature-verified callers
should no-op with 200 so Wompi does not retry; Tickets routing is unchanged
in wompi_webhook_router_service.
"""
import logging
from typing import Any, Dict

from fastapi import BackgroundTasks

logger = logging.getLogger(__name__)


async def handle_transaction_updated(
    body: Dict[str, Any],
    background_tasks: BackgroundTasks,
    provider_environment: str = "prod",
) -> None:
    """
    No-op for Colombia billing after Paddle-only (#798).

    Caller must still verify the webhook signature before invoking this.
    Historical payment_event / metadata rows are left untouched.
    """
    transaction = body.get("data", {}).get("transaction", {}) or {}
    status = str(transaction.get("status", "")).upper()
    level = logging.WARNING if status == "APPROVED" else logging.INFO
    logger.log(
        level,
        "Wompi Colombia billing deprecated (#798): no-op env=%s status=%s tx=%s link=%s",
        provider_environment,
        status,
        transaction.get("id"),
        transaction.get("payment_link_id"),
    )
    return

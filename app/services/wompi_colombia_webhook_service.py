"""
Colombia billing — Wompi transaction.updated handler (api-warolabs #353).

Extracted from billing.wompi_webhook so the central router and legacy
POST /billing/webhook share the same activation logic.
"""
import logging
from typing import Any, Dict

from fastapi import BackgroundTasks

from app.config import settings
from app.database import get_db_connection
from app.services import billing_email_service, billing_service, billing_webhook_service

logger = logging.getLogger(__name__)


async def handle_transaction_updated(
    body: Dict[str, Any],
    background_tasks: BackgroundTasks,
) -> None:
    """
    Process a verified Wompi transaction.updated event for Colombia billing.

    Caller must verify the signature and filter non-transaction events first.
    """
    transaction = body.get("data", {}).get("transaction", {})
    wompi_status = transaction.get("status", "").upper()
    payment_link_id = transaction.get("payment_link_id", "")
    transaction_id = str(transaction.get("id", ""))
    amount_cents = transaction.get("amount_in_cents", 0)

    logger.info(
        "Wompi Colombia transaction: link=%s status=%s tx=%s",
        payment_link_id,
        wompi_status,
        transaction_id,
    )

    if not payment_link_id:
        return

    if wompi_status == "APPROVED":
        async with get_db_connection() as conn:
            tenant_info = await billing_service.activate_tenant_subscription(
                conn,
                gateway_reference=payment_link_id,
                payment_id=transaction_id,
                amount=amount_cents / 100,
                currency="COP",
            )
        if tenant_info:
            background_tasks.add_task(
                billing_email_service.send_payment_renewed_email,
                tenant_name=tenant_info["tenant_name"],
                tenant_email=tenant_info["tenant_email"],
                next_period_end=tenant_info["next_period_end"],
            )
            background_tasks.add_task(
                billing_webhook_service.send_payment_approved_webhook,
                tenant_id=tenant_info["tenant_id"],
                subscription_id=tenant_info["subscription_id"],
                tenant_name=tenant_info["tenant_name"],
                tenant_email=tenant_info["tenant_email"],
                plan_name=tenant_info["plan_name"],
                amount=amount_cents / 100,
                currency="COP",
                next_period_end=tenant_info["next_period_end"],
                gateway_reference=payment_link_id,
                transaction_id=transaction_id,
            )

    elif wompi_status in ("DECLINED", "VOIDED", "ERROR"):
        async with get_db_connection() as conn:
            tenant_info = await billing_service.mark_subscription_past_due(
                conn, payment_link_id, "payment_rejected"
            )
        if tenant_info:
            background_tasks.add_task(
                billing_email_service.send_payment_rejected_email,
                tenant_name=tenant_info["tenant_name"],
                tenant_email=tenant_info["tenant_email"],
                billing_url=f"{settings.frontend_url}/gestion/billing",
            )

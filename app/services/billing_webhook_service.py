"""Outgoing webhook for billing events (issue #156).

Fires once per successful subscription payment. Triggered as a FastAPI
``BackgroundTask`` from the Wompi webhook handler — runs after the HTTP
response is sent so it never blocks Wompi's retry policy.

Failures are logged at WARNING and swallowed: webhook delivery problems
must NEVER roll back the subscription activation that has already
committed to the DB.
"""
import logging
from typing import Optional
from urllib.parse import urlparse

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


async def send_payment_approved_webhook(
    *,
    tenant_id: str,
    subscription_id: str,
    tenant_name: str,
    tenant_email: Optional[str],
    plan_name: str,
    amount: float,
    currency: str,
    next_period_end: str,
    gateway_reference: str,
    transaction_id: str,
) -> None:
    """POST a ``subscription.payment_approved`` JSON payload to
    ``BILLING_WEBHOOK_URL``.

    No-op when the env var is unset/empty (zero overhead, no error log).
    """
    url = settings.billing_webhook_url
    if not url:
        return

    # Log only the hostname — the URL may contain a token (Discord, etc.).
    host = urlparse(url).hostname or "<unknown>"

    # Discord webhooks reject arbitrary JSON — they need `content` or `embeds`.
    # Detect Discord URLs and format accordingly; everything else gets the raw
    # generic payload.
    if host and host.endswith("discord.com"):
        payload = {
            "embeds": [{
                "title": "💳 Subscription payment approved",
                "color": 3066993,
                "fields": [
                    {"name": "Tenant", "value": f"{tenant_name} (`{tenant_id}`)", "inline": False},
                    {"name": "Email", "value": tenant_email or "—", "inline": True},
                    {"name": "Plan", "value": plan_name, "inline": True},
                    {"name": "Amount", "value": f"{amount:,.0f} {currency}", "inline": True},
                    {"name": "Next period end", "value": next_period_end, "inline": True},
                    {"name": "Gateway ref", "value": f"`{gateway_reference}`", "inline": False},
                    {"name": "Transaction", "value": f"`{transaction_id}`", "inline": False},
                ],
            }],
        }
    else:
        payload = {
            "event": "subscription.payment_approved",
            "tenant_id": tenant_id,
            "subscription_id": subscription_id,
            "tenant_name": tenant_name,
            "tenant_email": tenant_email,
            "plan_name": plan_name,
            "amount": amount,
            "currency": currency,
            "next_period_end": next_period_end,
            "gateway_reference": gateway_reference,
            "transaction_id": transaction_id,
        }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
        logger.info(
            "billing_webhook: delivered host=%s tenant=%s amount=%s",
            host, tenant_id, amount,
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "billing_webhook: delivery failed (non-fatal) host=%s tenant=%s err=%s",
            host, tenant_id, type(exc).__name__,
        )
    except Exception as exc:
        logger.warning(
            "billing_webhook: unexpected error (non-fatal) host=%s tenant=%s err=%s",
            host, tenant_id, type(exc).__name__,
        )

"""
Wompi central ingress — classify transaction.updated and dispatch (#353).

Routing rules: docs/payments/wompi-webhook-routing.md (#352).
"""
import logging
from enum import Enum
from typing import Any, Dict
from uuid import UUID

import httpx
from fastapi import BackgroundTasks, HTTPException

from app.config import settings
from app.database import get_db_connection
from app.services import wompi_colombia_webhook_service, wompi_service

logger = logging.getLogger(__name__)

BILLING_REDIRECT_MARKER = "warocol.com/billing"
TICKETS_REFERENCE_PREFIX = "WT-"


class WompiRoute(str, Enum):
    TICKETS = "tickets"
    COLOMBIA = "colombia"
    UNKNOWN = "unknown"


async def classify_transaction_updated(body: Dict[str, Any]) -> WompiRoute:
    """Return dispatch target per routing matrix (epic #351 / doc #352)."""
    transaction = body.get("data", {}).get("transaction", {}) or {}
    reference = (transaction.get("reference") or "").strip()

    if reference.startswith(TICKETS_REFERENCE_PREFIX):
        return WompiRoute.TICKETS

    payment_link_id = (transaction.get("payment_link_id") or "").strip()
    if payment_link_id and await _gateway_reference_exists(payment_link_id):
        return WompiRoute.COLOMBIA

    redirect_url = (transaction.get("redirect_url") or "").lower()
    if BILLING_REDIRECT_MARKER in redirect_url:
        return WompiRoute.COLOMBIA

    sku = (transaction.get("sku") or "").strip()
    if sku and await _tenant_id_exists(sku):
        return WompiRoute.COLOMBIA

    return WompiRoute.UNKNOWN


async def _gateway_reference_exists(gateway_reference: str) -> bool:
    async with get_db_connection(use_transaction=False) as conn:
        row = await conn.fetchrow(
            """
            SELECT 1
            FROM billing_payment_attempts
            WHERE provider = 'wompi' AND provider_reference = $1
            UNION ALL
            SELECT 1
            FROM tenant_subscriptions
            WHERE gateway_reference = $1
            LIMIT 1
            """,
            gateway_reference,
        )
    return row is not None


async def _tenant_id_exists(sku: str) -> bool:
    try:
        tenant_id = UUID(sku)
    except ValueError:
        return False
    async with get_db_connection(use_transaction=False) as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM tenants WHERE id = $1 LIMIT 1",
            tenant_id,
        )
    return row is not None


async def forward_to_tickets(body: Dict[str, Any]) -> None:
    """
    HTTP-forward raw Wompi JSON to api.warotickets.

    Requires api_warotickets#46 internal auth on the receiver; sends
    X-Wompi-Forward-Secret when WOMPI_WEBHOOK_FORWARD_SECRET is set.
    """
    base_url = (settings.warotickets_api_url or "").rstrip()
    if not base_url:
        logger.error(
            "Wompi router: WAROTICKETS_API_URL not configured — cannot forward to Tickets"
        )
        raise HTTPException(
            status_code=503,
            detail="Tickets forward URL not configured",
        )

    url = f"{base_url}/payments/webhooks/wompi"
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    secret = settings.wompi_webhook_forward_secret
    if secret:
        headers["X-Wompi-Forward-Secret"] = secret
    else:
        logger.warning(
            "Wompi router: WOMPI_WEBHOOK_FORWARD_SECRET unset — forwarding without internal auth"
        )

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, json=body, headers=headers)
    except httpx.RequestError as exc:
        logger.error("Wompi router: Tickets forward failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="Failed to forward webhook to Tickets",
        ) from exc

    if response.status_code >= 400:
        logger.error(
            "Wompi router: Tickets returned %s — %s",
            response.status_code,
            response.text[:500],
        )
        raise HTTPException(
            status_code=502,
            detail="Tickets webhook handler rejected the forward",
        )

    logger.info("Wompi router: forwarded to Tickets (%s)", response.status_code)


async def dispatch_verified_event(
    body: Dict[str, Any],
    background_tasks: BackgroundTasks,
    expected_environment: str = wompi_service.WOMPI_PROD_ENVIRONMENT,
) -> Dict[str, Any]:
    """
    Verify signature, classify, and dispatch a Wompi event.

    Returns the HTTP response body for the ingress endpoint.
    """
    event = body.get("event", "")
    logger.info(
        "Wompi ingress: event=%s environment=%s",
        event,
        expected_environment,
    )

    if not wompi_service.verify_event_signature(
        body, expected_environment=expected_environment
    ):
        logger.warning("Wompi ingress: invalid signature")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    if event != "transaction.updated":
        return {"received": True}

    try:
        route = await classify_transaction_updated(body)
    except Exception as exc:
        logger.exception("Wompi ingress: classification failed")
        raise HTTPException(
            status_code=500,
            detail="Webhook classification failed",
        ) from exc
    transaction = body.get("data", {}).get("transaction", {}) or {}

    try:
        if route == WompiRoute.TICKETS:
            if expected_environment != wompi_service.WOMPI_PROD_ENVIRONMENT:
                logger.warning(
                    "Wompi sandbox ingress: ignoring non-Colombia reference"
                )
                return {"received": True, "classification": "unknown"}
            await forward_to_tickets(body)
            return {"status": "received"}

        if route == WompiRoute.COLOMBIA:
            await wompi_colombia_webhook_service.handle_transaction_updated(
                body,
                background_tasks,
                provider_environment=expected_environment,
            )
            return {"received": True}

        logger.warning(
            "Wompi ingress: unknown classification — tx=%s ref=%s link=%s redirect=%s sku=%s amount=%s",
            transaction.get("id"),
            transaction.get("reference"),
            transaction.get("payment_link_id"),
            transaction.get("redirect_url"),
            transaction.get("sku"),
            transaction.get("amount_in_cents"),
        )
        return {"received": True, "classification": "unknown"}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Wompi ingress: dispatch failed route=%s", route.value)
        raise HTTPException(
            status_code=500,
            detail="Webhook dispatch failed",
        ) from exc

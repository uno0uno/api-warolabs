"""
MercadoPago Service — issue #60

Wraps the MercadoPago Preapproval API for recurring subscription payments.
Uses httpx (already in requirements) — no SDK required.

API docs: https://www.mercadopago.com.co/developers/es/docs/subscriptions
"""
import hashlib
import hmac
import logging
from typing import Any, Dict, Optional
from uuid import UUID

import httpx
from fastapi import HTTPException

from app.config import settings

logger = logging.getLogger(__name__)

MP_API_BASE = "https://api.mercadopago.com"


def _get_headers() -> Dict[str, str]:
    """Return auth headers for MP API calls."""
    if not settings.mp_access_token:
        raise HTTPException(
            status_code=503,
            detail="MercadoPago no está configurado (MP_ACCESS_TOKEN faltante)",
        )
    return {
        "Authorization": f"Bearer {settings.mp_access_token}",
        "Content-Type": "application/json",
        "X-Idempotency-Key": "",  # set per-call when needed
    }


async def create_preapproval(
    plan_name: str,
    transaction_amount: float,
    billing_cycle: str,
    tenant_id: UUID,
    tenant_email: str,
    back_url: str,
) -> Dict[str, Any]:
    """
    Create a MercadoPago Preapproval (recurring subscription).

    Returns the full MP response including `id` (mp_preapproval_id) and
    the appropriate checkout URL for the configured environment.

    billing_cycle must be 'monthly' or 'annual'.
    Raises HTTP 422 if tenant_email is empty.
    Raises HTTP 502 if the MP API call fails.
    """
    if not tenant_email:
        raise HTTPException(
            status_code=422,
            detail="El tenant no tiene email registrado. Agrega un email antes de suscribirte.",
        )

    frequency = 1 if billing_cycle == "monthly" else 12
    frequency_type = "months"

    payload: Dict[str, Any] = {
        "payer_email": tenant_email,
        "reason": plan_name,
        "external_reference": str(tenant_id),
        "back_url": back_url,
        "auto_recurring": {
            "frequency": frequency,
            "frequency_type": frequency_type,
            "transaction_amount": transaction_amount,
            "currency_id": "COP",
        },
    }

    headers = _get_headers()
    headers["X-Idempotency-Key"] = f"subscribe-{tenant_id}-{billing_cycle}"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{MP_API_BASE}/preapproval",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        logger.error(
            "MP API error creating preapproval: status=%s body=%s",
            exc.response.status_code,
            exc.response.text,
        )
        raise HTTPException(
            status_code=502,
            detail={
                "error": "mp_api_error",
                "mp_status": exc.response.status_code,
                "message": "Error al crear la suscripción en MercadoPago",
            },
        )
    except httpx.RequestError as exc:
        logger.error("MP API connection error: %s", str(exc))
        raise HTTPException(
            status_code=502,
            detail={"error": "mp_connection_error", "message": str(exc)},
        )

    # Choose URL based on environment
    is_sandbox = settings.mp_environment.lower() == "sandbox"
    checkout_url: str = (
        data.get("sandbox_init_point") or data.get("init_point", "")
        if is_sandbox
        else data.get("init_point", "")
    )

    logger.info(
        "MP preapproval created: id=%s tenant=%s plan=%s",
        data.get("id"),
        tenant_id,
        plan_name,
    )

    return {
        "mp_preapproval_id": data["id"],
        "checkout_url": checkout_url,
        "status": data.get("status", "pending"),
    }


async def cancel_preapproval(preapproval_id: str) -> bool:
    """
    Cancel a MercadoPago Preapproval by setting status='cancelled'.
    Returns True on success, raises HTTP 502 on MP API error.
    """
    headers = _get_headers()
    headers["X-Idempotency-Key"] = f"cancel-{preapproval_id}"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.patch(
                f"{MP_API_BASE}/preapproval/{preapproval_id}",
                headers=headers,
                json={"status": "cancelled"},
            )
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.error(
            "MP API error cancelling preapproval %s: status=%s body=%s",
            preapproval_id,
            exc.response.status_code,
            exc.response.text,
        )
        raise HTTPException(
            status_code=502,
            detail={
                "error": "mp_api_error",
                "message": "Error al cancelar la suscripción en MercadoPago",
            },
        )
    except httpx.RequestError as exc:
        logger.error("MP API connection error: %s", str(exc))
        raise HTTPException(
            status_code=502,
            detail={"error": "mp_connection_error", "message": str(exc)},
        )

    logger.info("MP preapproval cancelled: id=%s", preapproval_id)
    return True


async def get_preapproval_status(preapproval_id: str) -> str:
    """
    Fetch the current status of a MercadoPago Preapproval.
    Returns the status string (pending, authorized, cancelled, paused, expired).
    """
    headers = _get_headers()

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                f"{MP_API_BASE}/preapproval/{preapproval_id}",
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        logger.error(
            "MP API error fetching preapproval %s: status=%s",
            preapproval_id,
            exc.response.status_code,
        )
        raise HTTPException(
            status_code=502,
            detail={"error": "mp_api_error", "message": "Error al consultar MP"},
        )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail={"error": "mp_connection_error", "message": str(exc)},
        )

    return data.get("status", "unknown")


async def get_payment_status(payment_id: str) -> Dict[str, Any]:
    """
    Fetch details of a MercadoPago payment event.

    Used by the webhook handler to verify payment status and extract amount.
    Returns a dict with: status, transaction_amount, currency_id, external_reference.

    external_reference is the tenant_id (UUID string) set when creating the preapproval.
    Raises HTTP 502 on MP API error.
    """
    headers = _get_headers()

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                f"{MP_API_BASE}/v1/payments/{payment_id}",
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        logger.error(
            "MP API error fetching payment %s: status=%s",
            payment_id,
            exc.response.status_code,
        )
        raise HTTPException(
            status_code=502,
            detail={"error": "mp_api_error", "message": "Error al consultar pago en MP"},
        )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail={"error": "mp_connection_error", "message": str(exc)},
        )

    return {
        "status": data.get("status", "unknown"),
        "transaction_amount": data.get("transaction_amount", 0),
        "currency_id": data.get("currency_id", "COP"),
        "external_reference": data.get("external_reference"),  # tenant_id UUID string
    }


def verify_webhook_signature(
    x_signature: Optional[str],
    x_request_id: Optional[str],
    data_id: Optional[str],
    secret: str,
) -> bool:
    """
    Verify MercadoPago webhook signature using HMAC-SHA256.

    MP sends: X-Signature: ts=<timestamp>,v1=<hash>
    Manifest: f"id:{data_id};request-id:{x_request_id};ts:{ts};"

    Returns True if signature is valid, False otherwise.
    """
    if not x_signature:
        return False

    ts: Optional[str] = None
    v1: Optional[str] = None

    for part in x_signature.split(","):
        part = part.strip()
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k.strip() == "ts":
            ts = v.strip()
        elif k.strip() == "v1":
            v1 = v.strip()

    if not ts or not v1:
        return False

    manifest = f"id:{data_id};request-id:{x_request_id};ts:{ts};"
    computed = hmac.new(
        secret.encode("utf-8"),
        manifest.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(computed, v1)

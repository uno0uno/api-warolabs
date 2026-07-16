"""
Wompi Payment Gateway — WARO COLOMBIA billing (issue #60)

Reemplaza MercadoPago. Usa Payment Links de Wompi para cobros únicos
(mensual/anual). El webhook activa la suscripción al confirmar el pago.

Docs: https://docs.wompi.co/docs/colombia/links-de-pago/
"""
import hashlib
import hmac
import logging
from datetime import datetime, timedelta
from typing import Any, Dict
from uuid import UUID

import httpx
from fastapi import HTTPException

from app.config import settings

logger = logging.getLogger(__name__)

WOMPI_SANDBOX_URL = "https://sandbox.wompi.co/v1"
WOMPI_PRODUCTION_URL = "https://production.wompi.co/v1"

WOMPI_PROD_ENVIRONMENT = "prod"
WOMPI_TEST_ENVIRONMENT = "test"


def _base_url() -> str:
    return WOMPI_PRODUCTION_URL if settings.wompi_environment == "production" else WOMPI_SANDBOX_URL


def configured_event_environment() -> str:
    """Map the configured Wompi API runtime to the signed event label."""
    return (
        WOMPI_PROD_ENVIRONMENT
        if settings.wompi_environment == "production"
        else WOMPI_TEST_ENVIRONMENT
    )


def _headers() -> Dict[str, str]:
    if not settings.wompi_private_key:
        raise HTTPException(
            status_code=503,
            detail="Wompi no está configurado (WOMPI_PRIVATE_KEY faltante)",
        )
    return {
        "Authorization": f"Bearer {settings.wompi_private_key}",
        "Content-Type": "application/json",
    }


async def create_payment_link(
    plan_name: str,
    amount_in_cents: int,
    billing_cycle: str,
    sku: UUID,
    redirect_url: str,
) -> Dict[str, Any]:
    """
    Crea un Payment Link de Wompi para el pago de suscripción.

    Retorna: { wompi_link_id, checkout_url }
    """
    if isinstance(amount_in_cents, bool) or not isinstance(amount_in_cents, int):
        raise HTTPException(status_code=422, detail="El monto de Wompi debe ser entero")
    if amount_in_cents <= 0:
        raise HTTPException(status_code=422, detail="El monto de Wompi debe ser positivo")
    if billing_cycle != "annual":
        raise HTTPException(status_code=422, detail="Wompi onboarding solo admite ciclo anual")
    cycle_label = "Mensual" if billing_cycle == "monthly" else "Anual"

    # 2 horas para completar el pago
    expiration = datetime.utcnow() + timedelta(hours=2)

    payload: Dict[str, Any] = {
        "name": f"WARO {plan_name} — {cycle_label}",
        "description": f"Suscripción WARO Colombia · Plan {plan_name} {cycle_label}",
        "single_use": True,
        "collect_shipping": False,
        "amount_in_cents": amount_in_cents,
        "currency": "COP",
        "expires_at": expiration.strftime("%Y-%m-%dT%H:%M:%S") + "Z",
        "redirect_url": redirect_url,
        "sku": str(sku),
        "collect_methods": [
            "CARD",
            "NEQUI",
            "PSE",
            "BANCOLOMBIA_TRANSFER",
            "BANCOLOMBIA_QR",
            "DAVIPLATA",
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{_base_url()}/payment_links",
                json=payload,
                headers=_headers(),
            )
            data = response.json()

            if response.status_code not in (200, 201):
                error_msg = data.get("error", {}).get("message", "Error desconocido")
                logger.error("Wompi API error: %s — %s", response.status_code, data)
                raise HTTPException(
                    status_code=502,
                    detail={"error": "wompi_api_error", "message": error_msg},
                )
    except httpx.RequestError as exc:
        logger.error("Wompi connection error: %s", exc)
        raise HTTPException(
            status_code=502,
            detail={"error": "wompi_connection_error", "message": str(exc)},
        )

    link_data = data.get("data", {})
    link_id = link_data.get("id")
    if not link_id:
        logger.error("Wompi response missing link ID: %s", data)
        raise HTTPException(
            status_code=502,
            detail={"error": "wompi_no_link_id", "message": "Wompi no devolvió un link ID"},
        )

    checkout_url = f"https://checkout.wompi.co/l/{link_id}"
    logger.info(
        "Wompi payment link created: id=%s tenant=%s plan=%s amount=%s",
        link_id, sku, plan_name, amount_in_cents,
    )

    return {"wompi_link_id": link_id, "checkout_url": checkout_url}


def verify_event_signature(
    event_data: Dict[str, Any],
    expected_environment: str = WOMPI_PROD_ENVIRONMENT,
) -> bool:
    """
    Verifica la firma del webhook de Wompi.

    Wompi incluye signature.checksum en el body del evento.
    """
    if expected_environment not in (WOMPI_PROD_ENVIRONMENT, WOMPI_TEST_ENVIRONMENT):
        logger.error("Wompi webhook environment is not supported")
        return False

    if event_data.get("environment") != expected_environment:
        logger.warning(
            "Wompi webhook environment mismatch: expected=%s received=%s",
            expected_environment,
            event_data.get("environment"),
        )
        return False

    events_secret = (
        settings.wompi_events_secret
        if expected_environment == WOMPI_PROD_ENVIRONMENT
        else settings.wompi_sandbox_events_secret
    )
    if not events_secret:
        logger.error(
            "Wompi events secret is not configured for environment=%s",
            expected_environment,
        )
        return False

    signature_data = event_data.get("signature", {})
    properties = signature_data.get("properties", [])
    checksum = signature_data.get("checksum", "")

    if not checksum:
        return False

    transaction = event_data.get("data", {}).get("transaction", {})
    values = []
    for prop in properties:
        key = prop.replace("transaction.", "") if prop.startswith("transaction.") else prop
        values.append(str(transaction.get(key, "")))

    values.append(str(event_data.get("timestamp", "")))
    values.append(events_secret)

    computed = hashlib.sha256("".join(values).encode()).hexdigest()
    return hmac.compare_digest(computed, checksum)


def map_status(wompi_status: str) -> str:
    """Mapea el status de Wompi al status interno de suscripción."""
    return {
        "APPROVED": "active",
        "PENDING": "pending",
        "DECLINED": "cancelled",
        "VOIDED": "cancelled",
        "ERROR": "cancelled",
    }.get(wompi_status.upper(), "pending")


async def get_transaction(transaction_id: str) -> Dict[str, Any]:
    """
    Consulta el estado de una transacción en la API de Wompi.
    Retorna el objeto transaction con status, payment_link_id, etc.
    """
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                f"{_base_url()}/transactions/{transaction_id}",
                headers=_headers(),
            )
            data = response.json()

            if response.status_code != 200:
                logger.error("Wompi get_transaction error: %s — %s", response.status_code, data)
                raise HTTPException(
                    status_code=502,
                    detail={"error": "wompi_api_error", "message": "No se pudo consultar la transacción"},
                )

            return data.get("data", {})

    except httpx.RequestError as exc:
        logger.error("Wompi connection error getting transaction: %s", exc)
        raise HTTPException(
            status_code=502,
            detail={"error": "wompi_connection_error", "message": str(exc)},
        )

"""
Paddle Billing — WARO SaaS checkout + webhooks (epic #793 / #805).

Resolves monthly Paddle price IDs from billing_pricing segments (#806).
Verifies Paddle-Signature webhooks and activates subscriptions via billing_service.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import UUID

import httpx
from fastapi import HTTPException

from app.config import settings
from app.core.billing_pricing import (
    PriceOffer,
    ProviderEnvironment,
)

logger = logging.getLogger(__name__)

PADDLE_LIVE_API = "https://api.paddle.com"
PADDLE_SANDBOX_API = "https://sandbox-api.paddle.com"


def _api_base(environment: ProviderEnvironment) -> str:
    return PADDLE_SANDBOX_API if environment == "test" else PADDLE_LIVE_API


def _api_key(environment: ProviderEnvironment) -> Optional[str]:
    if environment == "test":
        return settings.paddle_api_key_sandbox
    return settings.paddle_api_key_live


def _webhook_secret(environment: ProviderEnvironment) -> Optional[str]:
    if environment == "test":
        return settings.paddle_webhook_secret_sandbox
    return settings.paddle_webhook_secret_live


def configured_price_id(offer: PriceOffer, environment: ProviderEnvironment) -> str:
    """Prefer monthly env price IDs; annual env only if monthly unset; else placeholders."""
    monthly_mapping = {
        ("usd_9", "test"): settings.paddle_price_usd_9_monthly_test,
        ("usd_9", "prod"): settings.paddle_price_usd_9_monthly_live,
        ("usd_30", "test"): settings.paddle_price_usd_30_monthly_test,
        ("usd_30", "prod"): settings.paddle_price_usd_30_monthly_live,
        ("eur_30", "test"): settings.paddle_price_eur_30_monthly_test,
        ("eur_30", "prod"): settings.paddle_price_eur_30_monthly_live,
    }
    annual_mapping = {
        ("usd_9", "test"): settings.paddle_price_usd_9_annual_test,
        ("usd_9", "prod"): settings.paddle_price_usd_9_annual_live,
        ("usd_30", "test"): settings.paddle_price_usd_30_annual_test,
        ("usd_30", "prod"): settings.paddle_price_usd_30_annual_live,
        ("eur_30", "test"): settings.paddle_price_eur_30_annual_test,
        ("eur_30", "prod"): settings.paddle_price_eur_30_annual_live,
    }
    key = (offer.segment, environment)
    monthly = monthly_mapping.get(key)
    if monthly and str(monthly).strip():
        return str(monthly).strip()
    annual = annual_mapping.get(key)
    if annual and str(annual).strip():
        return str(annual).strip()
    return offer.paddle_price_id(environment)


def require_usable_price_id(price_id: str, environment: ProviderEnvironment) -> str:
    if price_id.startswith("TODO_"):
        if environment == "prod":
            raise HTTPException(
                status_code=422,
                detail="Paddle price ID no configurado para este segmento (prod)",
            )
        logger.warning("Using placeholder Paddle price_id=%s in test env", price_id)
    return price_id


def verify_paddle_signature(
    *,
    raw_body: bytes,
    signature_header: Optional[str],
    environment: ProviderEnvironment,
    max_skew_seconds: int = 300,
) -> None:
    """Verify Paddle-Signature (ts + h1 HMAC-SHA256). Raises 401 on failure."""
    secret = _webhook_secret(environment)
    if not secret:
        raise HTTPException(status_code=503, detail="Paddle webhook secret not configured")
    if not signature_header:
        raise HTTPException(status_code=401, detail="Missing Paddle-Signature")

    parts: Dict[str, str] = {}
    for item in signature_header.split(";"):
        item = item.strip()
        if "=" in item:
            k, v = item.split("=", 1)
            parts[k.strip()] = v.strip()
    ts = parts.get("ts")
    h1 = parts.get("h1")
    if not ts or not h1:
        raise HTTPException(status_code=401, detail="Invalid Paddle-Signature format")

    try:
        ts_int = int(ts)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid Paddle-Signature timestamp") from exc

    now = int(datetime.now(timezone.utc).timestamp())
    if abs(now - ts_int) > max_skew_seconds:
        raise HTTPException(status_code=401, detail="Paddle-Signature timestamp skew too large")

    payload = f"{ts}:".encode("utf-8") + raw_body
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, h1):
        raise HTTPException(status_code=401, detail="Invalid Paddle webhook signature")


async def create_checkout(
    *,
    offer: PriceOffer,
    environment: ProviderEnvironment,
    tenant_id: UUID,
    plan_id: UUID,
    billing_cycle: str,
    redirect_url: str,
    customer_email: Optional[str] = None,
    attempt_id: Optional[UUID] = None,
) -> Dict[str, Any]:
    """
    Create a Paddle Billing transaction checkout for the monthly price (#807).

    Returns: checkout_url, paddle_transaction_id, gateway_reference, currency, amount_minor
    """
    price_id = require_usable_price_id(
        configured_price_id(offer, environment),
        environment,
    )
    custom_data: Dict[str, str] = {
        "tenant_id": str(tenant_id),
        "plan_id": str(plan_id),
        "billing_cycle": billing_cycle,
        "provider_environment": environment,
    }
    if attempt_id:
        custom_data["attempt_id"] = str(attempt_id)

    api_key = _api_key(environment)
    if not api_key or price_id.startswith("TODO_"):
        # Dev/sandbox without catalog: synthetic checkout (webhook tests mock activation).
        fake_id = f"txn_mock_{secrets.token_hex(8)}"
        sep = "&" if "?" in redirect_url else "?"
        return {
            "checkout_url": f"{redirect_url}{sep}paddle_txn={fake_id}",
            "paddle_transaction_id": fake_id,
            "gateway_reference": fake_id,
            "currency": offer.currency,
            "amount_minor": offer.monthly_amount_minor,
            "price_id": price_id,
            "mock": True,
        }

    body: Dict[str, Any] = {
        "items": [{"price_id": price_id, "quantity": 1}],
        "custom_data": custom_data,
        "checkout": {"url": redirect_url},
    }
    if customer_email:
        body["customer"] = {"email": customer_email}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{_api_base(environment)}/transactions",
                json=body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
    except httpx.HTTPError as exc:
        logger.exception("Paddle create transaction failed: %s", exc)
        raise HTTPException(status_code=502, detail="Error contactando Paddle") from exc

    if response.status_code >= 400:
        logger.error("Paddle checkout error %s: %s", response.status_code, response.text[:500])
        raise HTTPException(status_code=502, detail="Paddle rechazó la creación del checkout")

    data = response.json().get("data") or {}
    txn_id = data.get("id")
    checkout = data.get("checkout") or {}
    checkout_url = checkout.get("url")
    if not txn_id or not checkout_url:
        raise HTTPException(status_code=502, detail="Respuesta incompleta de Paddle")

    return {
        "checkout_url": checkout_url,
        "paddle_transaction_id": txn_id,
        "gateway_reference": txn_id,
        "currency": offer.currency,
        "amount_minor": offer.monthly_amount_minor,
        "price_id": price_id,
        "mock": False,
    }


def parse_period_anchor(event_data: Dict[str, Any]) -> datetime:
    for key in ("billed_at", "created_at", "updated_at"):
        raw = event_data.get(key)
        if not raw:
            continue
        text = str(raw).replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return datetime.now(timezone.utc)


def extract_transaction_event(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize Paddle webhook payload into activation fields."""
    event_type = str(payload.get("event_type") or payload.get("eventType") or "")
    data = payload.get("data") or {}
    custom = data.get("custom_data") or {}
    totals = data.get("details", {}).get("totals") or data.get("totals") or {}
    amount_minor = totals.get("grand_total") or totals.get("total") or 0
    try:
        amount_minor_int = int(amount_minor)
    except (TypeError, ValueError):
        amount_minor_int = 0
    currency = str(
        totals.get("currency_code")
        or data.get("currency_code")
        or "USD"
    ).upper()
    return {
        "event_type": event_type,
        "transaction_id": data.get("id"),
        "status": str(data.get("status") or "").lower(),
        "tenant_id": custom.get("tenant_id"),
        "plan_id": custom.get("plan_id"),
        "billing_cycle": custom.get("billing_cycle") or "monthly",
        "attempt_id": custom.get("attempt_id"),
        "provider_environment": custom.get("provider_environment") or "prod",
        "amount_minor": amount_minor_int,
        "currency": currency,
        "period_anchor": parse_period_anchor(data),
        "subscription_id": (data.get("subscription_id") or None),
    }


async def _notify_payment_approved(tenant_info: Dict[str, Any], *, amount: float, currency: str, gateway_reference: str, transaction_id: str) -> None:
    """Fire renewal email + outbound approved webhook after a successful activate."""
    from app.services import billing_email_service, billing_webhook_service

    if not tenant_info:
        return
    await billing_email_service.send_payment_renewed_email(
        tenant_name=tenant_info.get("tenant_name") or "",
        tenant_email=tenant_info.get("tenant_email"),
        next_period_end=tenant_info.get("next_period_end") or "",
    )
    await billing_webhook_service.send_payment_approved_webhook(
        tenant_id=tenant_info["tenant_id"],
        subscription_id=tenant_info["subscription_id"],
        tenant_name=tenant_info.get("tenant_name") or "",
        tenant_email=tenant_info.get("tenant_email"),
        plan_name=tenant_info.get("plan_name") or "",
        amount=amount,
        currency=currency,
        next_period_end=tenant_info.get("next_period_end"),
        gateway_reference=gateway_reference,
        transaction_id=transaction_id,
    )


async def _notify_payment_rejected(tenant_info: Dict[str, Any]) -> None:
    from app.config import settings
    from app.services import billing_email_service

    if not tenant_info or not tenant_info.get("tenant_email"):
        return
    await billing_email_service.send_payment_rejected_email(
        tenant_name=tenant_info.get("tenant_name") or "",
        tenant_email=tenant_info.get("tenant_email"),
        billing_url=f"{settings.frontend_url}/gestion/billing",
    )


def _schedule_background(background_tasks, coro_fn, *args, **kwargs) -> None:
    """Queue notify work after the webhook HTTP response (Paddle retry-safe)."""
    if background_tasks is None:
        logger.warning("Paddle notify skipped: BackgroundTasks missing for %s", coro_fn.__name__)
        return
    background_tasks.add_task(coro_fn, *args, **kwargs)


async def handle_verified_webhook(
    payload: Dict[str, Any],
    *,
    environment: ProviderEnvironment,
    background_tasks=None,
) -> Dict[str, Any]:
    """Process a signature-verified Paddle event. Returns summary dict."""
    from uuid import UUID as _UUID

    from app.database import get_db_connection
    from app.services import billing_service

    parsed = extract_transaction_event(payload)
    event_type = parsed["event_type"]
    status = parsed["status"]
    txn_id = parsed["transaction_id"]

    # Rely on transaction.completed/paid — subscription.activated uses sub_* ids.
    success_events = {
        "transaction.completed",
        "transaction.paid",
    }
    failed_events = {
        "transaction.payment_failed",
        "transaction.canceled",
        "transaction.cancelled",
    }

    if event_type in failed_events or status in {"failed", "canceled", "cancelled"}:
        logger.info("Paddle payment not successful event=%s status=%s txn=%s", event_type, status, txn_id)
        tenant_raw = parsed.get("tenant_id")
        if not tenant_raw:
            logger.warning(
                "Paddle failure missing tenant_id txn=%s sub=%s — cannot mark past_due",
                txn_id,
                parsed.get("subscription_id"),
            )
            return {"ok": True, "activated": False, "reason": "failed_or_cancelled"}
        try:
            tenant_id = _UUID(str(tenant_raw))
        except ValueError:
            return {"ok": True, "activated": False, "reason": "failed_or_cancelled"}

        async with get_db_connection() as conn:
            tenant_info = await billing_service.mark_subscription_past_due_by_tenant(
                conn,
                tenant_id,
                "payment_rejected",
                paddle_transaction_id=str(txn_id) if txn_id else None,
                paddle_subscription_id=(
                    str(parsed["subscription_id"]) if parsed.get("subscription_id") else None
                ),
            )
        if tenant_info:
            _schedule_background(background_tasks, _notify_payment_rejected, tenant_info)
        return {"ok": True, "activated": False, "reason": "failed_or_cancelled"}

    if event_type not in success_events:
        logger.info("Paddle event ignored event=%s status=%s", event_type, status)
        return {"ok": True, "activated": False, "reason": "ignored_event"}

    if status and status not in {"completed", "paid", "billed"}:
        logger.info("Paddle txn status not success event=%s status=%s", event_type, status)
        return {"ok": True, "activated": False, "reason": "ignored_event"}

    if not txn_id:
        logger.warning("Paddle webhook missing txn: %s", parsed)
        return {"ok": True, "activated": False, "reason": "missing_ids"}

    amount = (parsed["amount_minor"] or 0) / 100.0
    currency = str(parsed.get("currency") or "USD").upper()
    activated = False
    tenant_info = None
    onboarding = False
    reason = None

    async with get_db_connection() as conn:
        if parsed.get("attempt_id"):
            try:
                attempt_id = _UUID(str(parsed["attempt_id"]))
            except ValueError:
                return {"ok": True, "activated": False, "reason": "bad_attempt_id"}
            result = await billing_service.process_paddle_onboarding_payment(
                conn,
                attempt_id=attempt_id,
                transaction_id=str(txn_id),
                amount_minor=int(parsed["amount_minor"] or 0),
                currency=parsed["currency"],
                period_anchor=parsed["period_anchor"],
                provider_environment=environment,
                paddle_subscription_id=(
                    str(parsed["subscription_id"]) if parsed.get("subscription_id") else None
                ),
            )
            activated = bool(result.get("activated"))
            tenant_info = result.get("tenant_info")
            onboarding = True
            reason = result.get("reason")
        else:
            if not parsed["tenant_id"]:
                logger.warning("Paddle webhook missing tenant: %s", parsed)
                return {"ok": True, "activated": False, "reason": "missing_ids"}

            try:
                tenant_id = _UUID(str(parsed["tenant_id"]))
            except ValueError:
                return {"ok": True, "activated": False, "reason": "bad_tenant_id"}

            activated = await billing_service.activate_subscription_by_gateway_ref(
                conn,
                tenant_id=tenant_id,
                gateway_reference=str(txn_id),
                wompi_transaction_id="",
                amount=amount,
                period_anchor=parsed["period_anchor"],
                currency=parsed["currency"],
                paddle_transaction_id=str(txn_id),
                paddle_subscription_id=(
                    str(parsed["subscription_id"]) if parsed.get("subscription_id") else None
                ),
                provider="paddle",
                provider_environment=environment,
            )
            if activated:
                tenant_info = await billing_service.get_tenant_notify_info_after_activate(
                    conn, tenant_id=tenant_id
                )
            reason = None if activated else "not_activated"

    # Renewals only — onboarding first payment should not get "renovada" copy.
    if activated and tenant_info and not onboarding:
        _schedule_background(
            background_tasks,
            _notify_payment_approved,
            tenant_info,
            amount=amount,
            currency=currency,
            gateway_reference=str(txn_id),
            transaction_id=str(txn_id),
        )

    out: Dict[str, Any] = {
        "ok": True,
        "activated": bool(activated),
        "transaction_id": txn_id,
        "reason": reason,
    }
    if onboarding:
        out["onboarding"] = True
    return out


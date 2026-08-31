"""
Lemon Squeezy — WARO SaaS checkout + webhooks (epic #941 / batch #942).

Resolves monthly variant IDs from billing_pricing segments.
Verifies X-Signature webhooks and activates subscriptions via billing_service.
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

LS_API = "https://api.lemonsqueezy.com/v1"
PROVIDER = "lemon_squeezy"


def _api_key() -> Optional[str]:
    return settings.lemon_squeezy_api_key


def _store_id() -> Optional[str]:
    raw = settings.lemon_squeezy_store_id
    return str(raw).strip() if raw else None


def _webhook_secret(environment: ProviderEnvironment) -> Optional[str]:
    if environment == "test":
        return settings.lemon_squeezy_webhook_secret_sandbox
    return settings.lemon_squeezy_webhook_secret_live


def configured_variant_id(offer: PriceOffer, environment: ProviderEnvironment) -> str:
    """Prefer env variant IDs; else PriceOffer placeholders."""
    monthly_mapping = {
        ("usd_9", "test"): settings.lemon_squeezy_variant_usd_9_monthly_test,
        ("usd_9", "prod"): settings.lemon_squeezy_variant_usd_9_monthly_live,
        ("usd_30", "test"): settings.lemon_squeezy_variant_usd_30_monthly_test,
        ("usd_30", "prod"): settings.lemon_squeezy_variant_usd_30_monthly_live,
        ("eur_30", "test"): settings.lemon_squeezy_variant_eur_30_monthly_test,
        ("eur_30", "prod"): settings.lemon_squeezy_variant_eur_30_monthly_live,
    }
    key = (offer.segment, environment)
    monthly = monthly_mapping.get(key)
    if monthly and str(monthly).strip():
        return str(monthly).strip()
    return offer.lemon_squeezy_variant_id(environment)


def require_usable_variant_id(variant_id: str, environment: ProviderEnvironment) -> str:
    if variant_id.startswith("TODO_"):
        if environment == "prod":
            raise HTTPException(
                status_code=422,
                detail="Lemon Squeezy variant ID no configurado para este segmento (prod)",
            )
        logger.warning("Using placeholder Lemon Squeezy variant_id=%s in test env", variant_id)
    return variant_id


def verify_lemon_squeezy_signature(
    *,
    raw_body: bytes,
    signature_header: Optional[str],
    environment: ProviderEnvironment,
) -> None:
    """Verify X-Signature HMAC-SHA256 hex digest of the raw body."""
    secret = _webhook_secret(environment)
    if not secret:
        raise HTTPException(status_code=503, detail="Lemon Squeezy webhook secret not configured")
    if not signature_header:
        raise HTTPException(status_code=401, detail="Missing X-Signature")

    digest = hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    expected = digest.encode("utf-8")
    received = signature_header.encode("utf-8")
    if len(expected) != len(received) or not hmac.compare_digest(expected, received):
        raise HTTPException(status_code=401, detail="Invalid Lemon Squeezy webhook signature")


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
    Create a Lemon Squeezy checkout for the monthly variant.

    Returns: checkout_url, gateway_reference, ls_checkout_id, currency, amount_minor
    Activation remains webhooks, not the return page.
    """
    variant_id = require_usable_variant_id(
        configured_variant_id(offer, environment),
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

    api_key = _api_key()
    store_id = _store_id()
    if not api_key or not store_id or variant_id.startswith("TODO_"):
        fake_id = f"ls_chk_mock_{secrets.token_hex(8)}"
        sep = "&" if "?" in redirect_url else "?"
        return {
            "checkout_url": f"{redirect_url}{sep}ls_checkout={fake_id}",
            "ls_checkout_id": fake_id,
            "gateway_reference": fake_id,
            "currency": offer.currency,
            "amount_minor": offer.monthly_amount_minor,
            "variant_id": variant_id,
            "mock": True,
        }

    checkout_data: Dict[str, Any] = {"custom": custom_data}
    if customer_email:
        checkout_data["email"] = customer_email

    body: Dict[str, Any] = {
        "data": {
            "type": "checkouts",
            "attributes": {
                "checkout_data": checkout_data,
                "product_options": {
                    "redirect_url": redirect_url,
                    "enabled_variants": [int(variant_id) if str(variant_id).isdigit() else variant_id],
                },
                "test_mode": environment == "test",
            },
            "relationships": {
                "store": {
                    "data": {"type": "stores", "id": str(store_id)},
                },
                "variant": {
                    "data": {"type": "variants", "id": str(variant_id)},
                },
            },
        }
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{LS_API}/checkouts",
                json=body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "application/vnd.api+json",
                    "Content-Type": "application/vnd.api+json",
                },
            )
    except httpx.HTTPError as exc:
        logger.exception("Lemon Squeezy create checkout failed: %s", exc)
        raise HTTPException(status_code=502, detail="Error contactando Lemon Squeezy") from exc

    if response.status_code >= 400:
        logger.error(
            "Lemon Squeezy checkout error %s: %s",
            response.status_code,
            response.text[:500],
        )
        raise HTTPException(status_code=502, detail="Lemon Squeezy rechazó la creación del checkout")

    data = response.json().get("data") or {}
    checkout_id = str(data.get("id") or "")
    attrs = data.get("attributes") or {}
    checkout_url = attrs.get("url")
    if not checkout_id or not checkout_url:
        raise HTTPException(status_code=502, detail="Respuesta incompleta de Lemon Squeezy")

    gateway_reference = f"ls_chk_{checkout_id}"
    return {
        "checkout_url": checkout_url,
        "ls_checkout_id": checkout_id,
        "gateway_reference": gateway_reference,
        "currency": offer.currency,
        "amount_minor": offer.monthly_amount_minor,
        "variant_id": variant_id,
        "mock": False,
    }


def parse_period_anchor(attrs: Dict[str, Any]) -> datetime:
    for key in ("created_at", "updated_at", "renews_at"):
        raw = attrs.get(key)
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


def _amount_minor_from_attrs(attrs: Dict[str, Any]) -> int:
    for key in ("total", "subtotal", "price"):
        raw = attrs.get(key)
        if raw is None:
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    item = attrs.get("first_subscription_item") or {}
    for key in ("price", "unit_price"):
        raw = item.get(key)
        if raw is None:
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    first_order_item = attrs.get("first_order_item") or {}
    raw = first_order_item.get("price")
    if raw is not None:
        try:
            value = int(raw)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return 0


async def fetch_order_totals(order_id: str) -> Dict[str, Any]:
    """GET /v1/orders/{id} — subscription_created payloads omit money fields."""
    api_key = _api_key()
    if not api_key or not order_id:
        return {"amount_minor": 0, "currency": None}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{LS_API}/orders/{order_id}",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "application/vnd.api+json",
                    "Content-Type": "application/vnd.api+json",
                },
            )
    except httpx.HTTPError as exc:
        logger.warning("Lemon Squeezy get order failed order=%s: %s", order_id, exc)
        return {"amount_minor": 0, "currency": None}

    if response.status_code >= 400:
        logger.warning(
            "Lemon Squeezy get order %s → %s: %s",
            order_id,
            response.status_code,
            response.text[:300],
        )
        return {"amount_minor": 0, "currency": None}

    attrs = (response.json().get("data") or {}).get("attributes") or {}
    amount_minor = _amount_minor_from_attrs(attrs)
    amount_subtotal_minor = 0
    raw_sub = attrs.get("subtotal")
    if raw_sub is not None:
        try:
            amount_subtotal_minor = int(raw_sub)
        except (TypeError, ValueError):
            amount_subtotal_minor = 0
    currency = str(attrs.get("currency") or "").upper() or None
    return {
        "amount_minor": amount_minor,
        "amount_subtotal_minor": amount_subtotal_minor,
        "currency": currency,
    }


async def resolve_webhook_amount(
    parsed: Dict[str, Any],
    *,
    environment: ProviderEnvironment,
) -> Dict[str, Any]:
    """
    Fill amount/currency when subscription webhooks omit totals.

    Order: attrs → GET order by ls_order_id → offer monthly for tenant country.
    """
    amount_minor = int(parsed.get("amount_minor") or 0)
    amount_subtotal_minor = int(parsed.get("amount_subtotal_minor") or 0)
    currency = str(parsed.get("currency") or "USD").upper()
    if amount_minor > 0:
        return {
            "amount_minor": amount_minor,
            "amount_subtotal_minor": amount_subtotal_minor,
            "currency": currency,
        }

    order_id = parsed.get("ls_order_id")
    if order_id:
        totals = await fetch_order_totals(str(order_id))
        if int(totals.get("amount_minor") or 0) > 0:
            return {
                "amount_minor": int(totals["amount_minor"]),
                "amount_subtotal_minor": int(totals.get("amount_subtotal_minor") or 0),
                "currency": str(totals.get("currency") or currency).upper(),
            }

    tenant_raw = parsed.get("tenant_id")
    if tenant_raw:
        from uuid import UUID as _UUID

        from app.database import get_db_connection
        from app.services import billing_service
        from app.core.billing_pricing import resolve_price_offer

        try:
            tenant_id = _UUID(str(tenant_raw))
        except ValueError:
            tenant_id = None
        if tenant_id is not None:
            async with get_db_connection(use_transaction=False) as conn:
                ctx = await billing_service.get_tenant_billing_context(conn, tenant_id)
            offer = resolve_price_offer(ctx.get("country_code"))
            logger.info(
                "LS amount fallback to offer segment=%s env=%s tenant=%s",
                offer.segment,
                environment,
                tenant_id,
            )
            return {
                "amount_minor": offer.monthly_amount_minor,
                "amount_subtotal_minor": offer.monthly_amount_minor,
                "currency": offer.currency,
            }

    return {
        "amount_minor": 0,
        "amount_subtotal_minor": 0,
        "currency": currency,
    }


def extract_subscription_event(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize Lemon Squeezy webhook payload into activation fields.

    ``subscription_created`` → Subscription object (``data.type=subscriptions``).
    ``subscription_payment_*`` → Subscription Invoice (``data.type=subscription-invoices``):
    use ``attributes.subscription_id`` for the LS subscription and ``data.id`` as invoice id.
    """
    meta = payload.get("meta") or {}
    event_type = str(meta.get("event_name") or payload.get("event_name") or "")
    custom = meta.get("custom_data") or {}
    data = payload.get("data") or {}
    attrs = data.get("attributes") or {}
    data_type = str(data.get("type") or "").lower()
    status = str(attrs.get("status") or "").lower()
    currency = str(attrs.get("currency") or "USD").upper()

    ls_invoice_id: Optional[str] = None
    ls_subscription_id: Optional[str] = None
    order_id = attrs.get("order_id")
    gateway_reference: Optional[str] = None

    if data_type == "subscription-invoices" or event_type.startswith("subscription_payment_"):
        # Renewal / payment events: data.id is the invoice, not the subscription.
        if data.get("id") is not None:
            ls_invoice_id = str(data.get("id"))
        sub_raw = attrs.get("subscription_id")
        if sub_raw is not None:
            ls_subscription_id = str(sub_raw)
        gateway_reference = f"ls_inv_{ls_invoice_id}" if ls_invoice_id else (
            f"ls_sub_{ls_subscription_id}" if ls_subscription_id else None
        )
    else:
        # subscription_created / subscription_* lifecycle on Subscription object
        if data.get("id") is not None:
            ls_subscription_id = str(data.get("id"))
        if order_id is not None:
            gateway_reference = f"ls_ord_{order_id}"
        elif ls_subscription_id is not None:
            gateway_reference = f"ls_sub_{ls_subscription_id}"

    amount_minor = _amount_minor_from_attrs(attrs)
    amount_subtotal_minor = 0
    raw_sub = attrs.get("subtotal")
    if raw_sub is not None:
        try:
            amount_subtotal_minor = int(raw_sub)
        except (TypeError, ValueError):
            amount_subtotal_minor = 0

    return {
        "event_type": event_type,
        "data_type": data_type,
        "status": status,
        "tenant_id": custom.get("tenant_id"),
        "plan_id": custom.get("plan_id"),
        "billing_cycle": custom.get("billing_cycle") or "monthly",
        "attempt_id": custom.get("attempt_id"),
        "provider_environment": custom.get("provider_environment") or "prod",
        "amount_minor": amount_minor,
        "amount_subtotal_minor": amount_subtotal_minor,
        "currency": currency,
        "billing_reason": attrs.get("billing_reason"),
        "period_anchor": parse_period_anchor(attrs),
        "ls_subscription_id": ls_subscription_id,
        "ls_order_id": str(order_id) if order_id is not None else None,
        "ls_invoice_id": ls_invoice_id,
        "gateway_reference": gateway_reference,
        "transaction_id": gateway_reference,
    }


async def _notify_payment_approved(
    tenant_info: Dict[str, Any],
    *,
    amount: float,
    currency: str,
    gateway_reference: str,
    transaction_id: str,
) -> None:
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
    from app.config import settings as app_settings
    from app.services import billing_email_service

    if not tenant_info or not tenant_info.get("tenant_email"):
        return
    await billing_email_service.send_payment_rejected_email(
        tenant_name=tenant_info.get("tenant_name") or "",
        tenant_email=tenant_info.get("tenant_email"),
        billing_url=f"{app_settings.frontend_url}/gestion/billing",
    )


def _schedule_background(background_tasks, coro_fn, *args, **kwargs) -> None:
    if background_tasks is None:
        logger.warning("LS notify skipped: BackgroundTasks missing for %s", coro_fn.__name__)
        return
    background_tasks.add_task(coro_fn, *args, **kwargs)


async def handle_verified_webhook(
    payload: Dict[str, Any],
    *,
    environment: ProviderEnvironment,
    background_tasks=None,
) -> Dict[str, Any]:
    """Process a signature-verified Lemon Squeezy event."""
    from uuid import UUID as _UUID

    from app.database import get_db_connection
    from app.services import billing_service

    parsed = extract_subscription_event(payload)
    event_type = parsed["event_type"]
    status = parsed["status"]
    txn_id = parsed["transaction_id"]

    success_events = {
        "subscription_created",
        "subscription_payment_success",
    }
    failed_events = {
        "subscription_payment_failed",
        "subscription_expired",
    }

    if event_type in failed_events:
        logger.info(
            "LS payment not successful event=%s status=%s ref=%s",
            event_type,
            status,
            txn_id,
        )
        tenant_raw = parsed.get("tenant_id")
        if not tenant_raw:
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
                provider=PROVIDER,
                ls_order_id=parsed.get("ls_order_id"),
                ls_subscription_id=parsed.get("ls_subscription_id"),
            )
        if tenant_info:
            _schedule_background(background_tasks, _notify_payment_rejected, tenant_info)
        return {"ok": True, "activated": False, "reason": "failed_or_cancelled"}

    if event_type not in success_events:
        logger.info("LS event ignored event=%s status=%s", event_type, status)
        return {"ok": True, "activated": False, "reason": "ignored_event"}

    if (
        event_type == "subscription_created"
        and status
        and status not in {"active", "on_trial"}
    ):
        logger.info("LS status not success event=%s status=%s", event_type, status)
        return {"ok": True, "activated": False, "reason": "ignored_event"}

    if (
        event_type == "subscription_payment_success"
        and status
        and status not in {"paid"}
    ):
        logger.info("LS invoice status not paid event=%s status=%s", event_type, status)
        return {"ok": True, "activated": False, "reason": "ignored_event"}

    if not txn_id:
        logger.warning("LS webhook missing order/sub ref: %s", parsed)
        return {"ok": True, "activated": False, "reason": "missing_ids"}

    resolved = await resolve_webhook_amount(parsed, environment=environment)
    amount_minor = int(resolved["amount_minor"] or 0)
    amount = amount_minor / 100.0
    currency = str(resolved.get("currency") or parsed.get("currency") or "USD").upper()
    amount_subtotal_minor = int(
        resolved.get("amount_subtotal_minor")
        or parsed.get("amount_subtotal_minor")
        or 0
    ) or None
    if amount_minor <= 0:
        logger.warning("LS webhook could not resolve amount ref=%s", txn_id)
        return {"ok": True, "activated": False, "reason": "missing_amount"}
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
            result = await billing_service.process_mo_r_onboarding_payment(
                conn,
                attempt_id=attempt_id,
                transaction_id=str(txn_id),
                amount_minor=amount_minor,
                currency=currency,
                period_anchor=parsed["period_anchor"],
                provider_environment=environment,
                provider=PROVIDER,
                ls_subscription_id=parsed.get("ls_subscription_id"),
                ls_order_id=parsed.get("ls_order_id"),
                ls_invoice_id=parsed.get("ls_invoice_id"),
                amount_subtotal_minor=amount_subtotal_minor,
            )
            activated = bool(result.get("activated"))
            tenant_info = result.get("tenant_info")
            onboarding = True
            reason = result.get("reason")
        else:
            if not parsed["tenant_id"]:
                logger.warning("LS webhook missing tenant: %s", parsed)
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
                currency=currency,
                ls_order_id=parsed.get("ls_order_id"),
                ls_subscription_id=parsed.get("ls_subscription_id"),
                ls_invoice_id=parsed.get("ls_invoice_id"),
                provider=PROVIDER,
                provider_environment=environment,
            )
            if activated:
                tenant_info = await billing_service.get_tenant_notify_info_after_activate(
                    conn, tenant_id=tenant_id
                )
            reason = None if activated else "not_activated"

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


async def reconcile_checkout_status_for_tenant(
    *,
    tenant_id: UUID,
    checkout_id: Optional[str] = None,
    background_tasks=None,
) -> Dict[str, Any]:
    """
    Thank-you / poll stub (#942): read local subscription state.
    Activation remains webhook-driven; this never trusts the browser alone.
    """
    del background_tasks  # reserved for future LS API reconcile
    from app.database import get_db_connection
    from app.services import billing_service

    async with get_db_connection(use_transaction=False) as conn:
        access = await billing_service.get_subscription_access(tenant_id, conn)
        sub = await conn.fetchrow(
            """
            SELECT status, gateway_reference
            FROM tenant_subscriptions
            WHERE tenant_id = $1
            """,
            tenant_id,
        )

    status = (sub["status"] if sub else None)
    activated = status == "active"
    reason = None if activated else "awaiting_payment"
    return {
        "checkout_id": checkout_id,
        "ls_status": status,
        "activated": activated,
        "reason": reason,
        "subscription_status": status,
        "gateway_reference": (sub["gateway_reference"] if sub else None),
        "access_level": access.level,
        "waro_ready": access.level in {"full", "full_with_warning"},
    }

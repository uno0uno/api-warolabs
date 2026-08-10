"""
Billing Router — tenant-facing subscription flows (issue #60)

tenant_router: /billing — subscribe, status, cancel, webhook, access-status,
               grace-reminders cron.

Operator endpoints are gated by `require_module(Module.MI_PLAN)` (issue #185 /
Epic 2 #164). Two endpoints are intentionally NOT gated and carry an explicit
`# NOTE:` comment above their decorator: the Wompi webhook (signature-verified)
and the grace-reminders cron (X-Cron-Secret header).

The previous `/admin/billing/*` router was deleted in #185 — those endpoints
were dead code (no frontend consumer, no scripts) and a security risk under
RBAC enforcement.
"""
import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query, Request
from pydantic import BaseModel

from app.config import settings
from app.core.middleware import require_valid_session
from app.core.permissions import Module, require_module
from app.database import get_db_connection
from app.services import (
    billing_service,
    billing_email_service,
    legal_service,
    onboarding_service,
    paddle_service,
    wompi_service,
    wompi_colombia_webhook_service,
)

logger = logging.getLogger(__name__)

tenant_router = APIRouter(prefix="/billing", tags=["Billing"])


# ── Pydantic models ───────────────────────────────────────────────────────────
# Python 3.9 safe: Optional[X] from typing, no X | None syntax

class SubscribeBody(BaseModel):
    """Body for POST /billing/subscribe"""
    plan_id: UUID
    billing_cycle: str = "monthly"  # new subscriptions: monthly only (#807)
    payer_email: Optional[str] = None


# ── Tenant-facing billing endpoints (issue #60) ───────────────────────────────


@tenant_router.get(
    "/plans",
    dependencies=[Depends(require_module(Module.MI_PLAN))],
)
async def tenant_list_plans(request: Request):
    """List active subscription plans plus regional Paddle price_offer (#796)."""
    session = require_valid_session(request)
    from app.core.billing_pricing import resolve_price_offer

    async with get_db_connection(use_transaction=False) as conn:
        if session.lifecycle_status == "pending":
            await onboarding_service.ensure_onboarding_payment_ready(conn, session)
        plans = await billing_service.list_plans(conn)
        ctx = await billing_service.get_tenant_billing_context(conn, session.tenant_id)

    offer = resolve_price_offer(ctx.get("country_code"))
    return {
        "plans": [p for p in plans if p["is_active"]],
        "price_offer": {
            "segment": offer.segment,
            "currency": offer.currency,
            "monthly_amount_minor": offer.monthly_amount_minor,
            "annual_amount_minor": offer.annual_amount_minor,
            "monthly_amount": offer.monthly_amount_minor / 100.0,
            "annual_amount": offer.annual_amount_minor / 100.0,
        },
    }


@tenant_router.post(
    "/subscribe",
    status_code=201,
    dependencies=[Depends(require_module(Module.MI_PLAN))],
)
async def subscribe(body: SubscribeBody, request: Request):
    """
    Subscribe the authenticated tenant to a plan via Paddle checkout (#807).

    billing_cycle must be 'monthly' for new subscriptions.
    Active annuals with a future period end are blocked mid-period (#797).
    """
    if body.billing_cycle != "monthly":
        from fastapi import HTTPException
        raise HTTPException(
            status_code=422,
            detail="Las suscripciones nuevas solo estan disponibles con ciclo mensual (billing_cycle='monthly').",
        )

    session = require_valid_session(request)
    tenant_id = session.tenant_id
    is_pending_onboarding = session.lifecycle_status == "pending"

    from urllib.parse import urlparse
    from app.core.billing_pricing import resolve_price_offer, resolve_provider_environment

    parsed = urlparse(settings.frontend_url)
    frontend_host = f"{parsed.scheme}://{parsed.netloc}"
    redirect_url = f"{frontend_host}/billing/confirmacion"

    async with get_db_connection() as conn:
        if is_pending_onboarding:
            await onboarding_service.ensure_onboarding_payment_ready(conn, session)
        plan = await billing_service.get_plan_for_subscribe(conn, body.plan_id)
        if not is_pending_onboarding:
            await legal_service.ensure_current_terms_accepted(conn, tenant_id)
            await billing_service.ensure_subscribe_allowed(conn, tenant_id)

        ctx = await billing_service.get_tenant_billing_context(conn, tenant_id)
        offer = resolve_price_offer(ctx["country_code"])
        provider_environment = resolve_provider_environment(tenant_slug=ctx.get("slug"))

        if is_pending_onboarding:
            attempt_id = await billing_service.create_onboarding_payment_attempt(
                conn,
                tenant_id=tenant_id,
                plan_id=body.plan_id,
                amount_in_cents=offer.monthly_amount_minor,
                provider_environment=provider_environment,
                currency=offer.currency,
                provider="paddle",
            )
        else:
            paddle_result = await paddle_service.create_checkout(
                offer=offer,
                environment=provider_environment,
                tenant_id=tenant_id,
                plan_id=body.plan_id,
                billing_cycle=body.billing_cycle,
                redirect_url=redirect_url,
                customer_email=body.payer_email,
            )
            return await billing_service.subscribe_tenant(
                conn,
                tenant_id=tenant_id,
                plan_id=body.plan_id,
                billing_cycle=body.billing_cycle,
                checkout_url=paddle_result["checkout_url"],
                gateway_reference=paddle_result["gateway_reference"],
            )

    paddle_result = await paddle_service.create_checkout(
        offer=offer,
        environment=provider_environment,
        tenant_id=tenant_id,
        plan_id=body.plan_id,
        billing_cycle=body.billing_cycle,
        redirect_url=redirect_url,
        customer_email=body.payer_email,
        attempt_id=attempt_id,
    )
    async with get_db_connection() as conn:
        await billing_service.attach_onboarding_payment_link(
            conn,
            attempt_id=attempt_id,
            tenant_id=tenant_id,
            provider_reference=paddle_result["gateway_reference"],
            checkout_url=paddle_result["checkout_url"],
        )
    return {
        "attempt_id": str(attempt_id),
        "plan_id": str(body.plan_id),
        "checkout_url": paddle_result["checkout_url"],
        "gateway_reference": paddle_result["gateway_reference"],
        "amount_in_cents": offer.monthly_amount_minor,
        "currency": offer.currency,
        "billing_cycle": "monthly",
        "status": "pending",
        "provider": "paddle",
    }


@tenant_router.get(
    "/subscription",
    dependencies=[Depends(require_module(Module.MI_PLAN))],
)
async def get_my_subscription(request: Request):
    """Get the current subscription status for the authenticated tenant."""
    session = require_valid_session(request)
    async with get_db_connection(use_transaction=False) as conn:
        return await billing_service.get_tenant_subscription(conn, session.tenant_id)


@tenant_router.get(
    "/remaining-usage",
    dependencies=[Depends(require_module(Module.MI_PLAN))],
)
async def get_my_remaining_usage(request: Request):
    """Current-period remaining usage for the authenticated tenant."""
    session = require_valid_session(request)
    async with get_db_connection(use_transaction=False) as conn:
        return await billing_service.get_remaining_billing_usage(conn, session.tenant_id)


@tenant_router.get(
    "/verify-payment",
    dependencies=[Depends(require_module(Module.MI_PLAN))],
)
async def verify_payment(
    request: Request,
    transaction_id: str = Query(...),
):
    """
    Read-only status for a legacy Wompi transaction (#798).

    Does not activate or renew subscriptions — new billing is Paddle-only.
    Still requires the payment link to belong to the authenticated tenant.
    """
    session = require_valid_session(request)
    tenant_id = session.tenant_id

    transaction = await wompi_service.get_transaction(transaction_id)
    wompi_status = transaction.get("status", "PENDING").upper()
    internal_status = wompi_service.map_status(wompi_status)
    payment_link_id = transaction.get("payment_link_id")

    if not payment_link_id:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Payment reference not found")

    async with get_db_connection(use_transaction=False) as conn:
        belongs_to_tenant = await billing_service.payment_reference_belongs_to_tenant(
            conn,
            tenant_id=tenant_id,
            provider_reference=payment_link_id,
        )
    if not belongs_to_tenant:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Payment transaction not found")

    logger.info(
        "verify-payment read-only (#798): no Wompi activate tx=%s status=%s",
        transaction_id,
        wompi_status,
    )
    return {
        "status": internal_status,
        "wompi_status": wompi_status,
        "transaction_id": transaction_id,
        "payment_link_id": payment_link_id,
        "activation": "deprecated",
    }


@tenant_router.get(
    "/usage-history",
    dependencies=[Depends(require_module(Module.MI_PLAN))],
)
async def get_my_usage_history(
    request: Request,
    months: int = Query(12, ge=1, le=24, description="Número de meses a retornar"),
):
    """Monthly scan usage history for the authenticated tenant (last N months)."""
    session = require_valid_session(request)
    async with get_db_connection(use_transaction=False) as conn:
        return await billing_service.get_scan_monthly_history(session.tenant_id, conn, months)


@tenant_router.get(
    "/events",
    dependencies=[Depends(require_module(Module.MI_PLAN))],
)
async def get_my_billing_events(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """Paginated billing events for the authenticated tenant (tenant_id from session cookie)."""
    session = require_valid_session(request)
    async with get_db_connection(use_transaction=False) as conn:
        return await billing_service.list_tenant_billing_events(conn, session.tenant_id, limit, offset)


@tenant_router.delete(
    "/subscription",
    dependencies=[Depends(require_module(Module.MI_PLAN))],
)
async def cancel_my_subscription(request: Request):
    """
    Cancela la suscripción activa del tenant en la DB.
    Provider cancel (Paddle) is handled separately when wired; DB status is source of access.
    """
    session = require_valid_session(request)

    async with get_db_connection() as conn:
        gateway_reference = await billing_service.cancel_tenant_subscription(
            conn, session.tenant_id
        )

    return {"status": "cancelled", "gateway_reference": gateway_reference or None}


# NOTE: Authenticated by Wompi signature verification, not session.
# Do NOT add require_module() here — legacy URL still receives retries.
@tenant_router.post("/webhook", status_code=200)
async def wompi_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """
    Legacy Wompi webhook — signature verified, Colombia billing no-op (#798).

    Prefer POST /payments/webhooks/wompi for central routing (Tickets forward).
    """
    body = await request.json()
    event = body.get("event", "")

    logger.info("Wompi webhook received (deprecated billing): event=%s", event)

    if not wompi_service.verify_event_signature(body):
        logger.warning("Wompi webhook: firma inválida — rechazando")
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    if event != "transaction.updated":
        return {"received": True}

    await wompi_colombia_webhook_service.handle_transaction_updated(
        body, background_tasks
    )
    return {"received": True, "deprecated": True}


# ── Grace period & access control — issue #62 ────────────────────────────────


@tenant_router.get(
    "/access-status",
    dependencies=[Depends(require_module(Module.MI_PLAN))],
)
async def get_access_status(request: Request):
    """
    Return the subscription access level for the authenticated tenant.

    Levels:
      starter          — no paid subscription; permanent Starter plan
      full             — active subscription
      full_with_warning — past_due, ≤ 3 days overdue
      read_only        — past_due, 3-7 days overdue
      blocked          — pending checkout, past_due > 7 days, or cancelled/expired
    """
    session = require_valid_session(request)
    async with get_db_connection(use_transaction=False) as conn:
        access = await billing_service.get_subscription_access(session.tenant_id, conn)
        plan_slug = await billing_service.get_effective_plan_slug(conn, session.tenant_id)
        quotas = (
            await billing_service.get_effective_plan_quotas(conn, session.tenant_id)
            if plan_slug
            else {}
        )
        return {
            "level": access.level,
            "grace_days_remaining": access.grace_days_remaining,
            "subscription_status": access.subscription_status,
            "next_payment_date": access.next_payment_date,
            "message": access.message,
            "plan_slug": plan_slug,
            "quotas": quotas,
        }


# NOTE: Cron endpoint authenticated by X-Cron-Secret header, not session.
# Do NOT add require_module() here — it would break the grace-reminder job
# that runs from cron-job.org.
@tenant_router.post("/send-grace-reminders", status_code=200)
async def send_grace_reminders(
    request: Request,
    x_cron_secret: Optional[str] = Header(None, alias="x-cron-secret"),
):
    """
    Cron endpoint — send grace period reminder emails to past_due tenants.

    Protected by X-Cron-Secret header. Called by an external scheduler (e.g. cron-job.org).
    Returns a summary of sent / skipped / error counts.

    If CRON_SECRET is not configured, the endpoint runs without auth (dev mode).
    """
    if settings.cron_secret:
        if x_cron_secret != settings.cron_secret:
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="Invalid cron secret")

    async with get_db_connection() as conn:
        result = await billing_email_service.process_grace_reminders(conn)

    logger.info(
        "send_grace_reminders cron: sent=%d skipped=%d error=%d",
        result["sent"], result["skipped"], result["error"],
    )
    return result

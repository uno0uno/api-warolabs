"""
Billing Routers — admin CRUD (issue #61) + tenant subscription flows (issue #60)

router:        /admin/billing — plan CRUD, subscription management (admin)
tenant_router: /billing       — subscribe, status, cancel, webhook (tenant-facing)
"""
import logging
from decimal import Decimal
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Header, Query, Request
from pydantic import BaseModel

from app.config import settings
from app.core.middleware import require_valid_session
from app.database import get_db_connection
from app.services import billing_service, billing_email_service, mercadopago_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/billing", tags=["Billing Admin"])
tenant_router = APIRouter(prefix="/billing", tags=["Billing"])


# ── Pydantic models ───────────────────────────────────────────────────────────
# Python 3.9 safe: Optional[X] from typing, no X | None syntax

class PlanCreate(BaseModel):
    """Body for POST /admin/billing/plans"""
    name: str
    slug: str
    description: Optional[str] = None
    price_monthly: Decimal
    price_annual: Decimal
    scan_limit: int = 1000
    features: Dict[str, Any] = {}


class PlanUpdate(BaseModel):
    """Body for PATCH /admin/billing/plans/{plan_id} — all fields optional"""
    name: Optional[str] = None
    description: Optional[str] = None
    price_monthly: Optional[Decimal] = None
    price_annual: Optional[Decimal] = None
    scan_limit: Optional[int] = None
    features: Optional[Dict[str, Any]] = None


class SubscriptionUpdate(BaseModel):
    """Body for PATCH /admin/billing/subscriptions/{sub_id}"""
    status: Optional[str] = None
    plan_id: Optional[UUID] = None


# ── Plan endpoints ────────────────────────────────────────────────────────────

@router.get("/plans")
async def list_plans(request: Request):
    """List all subscription plans ordered by monthly price."""
    require_valid_session(request)
    async with get_db_connection(use_transaction=False) as conn:
        return await billing_service.list_plans(conn)


@router.post("/plans", status_code=201)
async def create_plan(body: PlanCreate, request: Request):
    """Create a new subscription plan. Returns 409 if slug already exists."""
    require_valid_session(request)
    async with get_db_connection() as conn:
        return await billing_service.create_plan(conn, body.model_dump())


@router.patch("/plans/{plan_id}")
async def update_plan(plan_id: UUID, body: PlanUpdate, request: Request):
    """Partially update a subscription plan. Returns 404 if not found."""
    require_valid_session(request)
    async with get_db_connection() as conn:
        return await billing_service.update_plan(
            conn, plan_id, body.model_dump(exclude_none=True)
        )


@router.delete("/plans/{plan_id}")
async def deactivate_plan(plan_id: UUID, request: Request):
    """
    Soft-delete a plan by setting is_active=false.
    The plan record is kept (referenced by existing subscriptions).
    Returns 404 if not found.
    """
    require_valid_session(request)
    async with get_db_connection() as conn:
        return await billing_service.deactivate_plan(conn, plan_id)


# ── Subscription endpoints ────────────────────────────────────────────────────

@router.get("/subscriptions")
async def list_subscriptions(request: Request):
    """List all tenant subscriptions with tenant name and plan details."""
    require_valid_session(request)
    async with get_db_connection(use_transaction=False) as conn:
        return await billing_service.list_subscriptions(conn)


@router.get("/subscriptions/{tenant_id}")
async def get_subscription(tenant_id: UUID, request: Request):
    """Get subscription details for a specific tenant. Returns 404 if not found."""
    require_valid_session(request)
    async with get_db_connection(use_transaction=False) as conn:
        return await billing_service.get_subscription_by_tenant(conn, tenant_id)


@router.patch("/subscriptions/{sub_id}/status")
async def update_subscription_status(
    sub_id: UUID, body: SubscriptionUpdate, request: Request
):
    """
    Manually update a subscription's status and/or plan.
    Status must be one of: pending, active, past_due, cancelled, expired.
    Returns 404 if not found, 422 if status is invalid.
    """
    require_valid_session(request)
    async with get_db_connection() as conn:
        return await billing_service.update_subscription(
            conn, sub_id, body.model_dump(exclude_none=True)
        )


# ── Usage & events ────────────────────────────────────────────────────────────

@router.get("/usage")
async def list_usage_summary(request: Request):
    """
    Scan usage summary for the current period across all tenants.
    Tenants with no scans appear with scans_used=0.
    """
    require_valid_session(request)
    async with get_db_connection(use_transaction=False) as conn:
        return await billing_service.list_usage_summary(conn)


@router.get("/events")
async def list_billing_events(
    request: Request,
    limit: int = Query(50, ge=1, le=200, description="Max results per page"),
    offset: int = Query(0, ge=0, description="Results to skip"),
):
    """Paginated billing events log, newest first."""
    require_valid_session(request)
    async with get_db_connection(use_transaction=False) as conn:
        return await billing_service.list_billing_events(conn, limit, offset)


# ── Tenant-facing billing endpoints (issue #60) ───────────────────────────────
# tenant_router prefix: /billing


class SubscribeBody(BaseModel):
    """Body for POST /billing/subscribe"""
    plan_id: UUID
    billing_cycle: str = "monthly"  # "monthly" | "annual"


@tenant_router.get("/plans")
async def tenant_list_plans(request: Request):
    """List active subscription plans (tenant-facing, read-only)."""
    require_valid_session(request)
    async with get_db_connection(use_transaction=False) as conn:
        plans = await billing_service.list_plans(conn)
        return [p for p in plans if p["is_active"]]


@tenant_router.post("/subscribe", status_code=201)
async def subscribe(body: SubscribeBody, request: Request):
    """
    Subscribe the authenticated tenant to a plan.

    1. Validates plan exists and is active
    2. Validates tenant has email (required by MP)
    3. Creates a MercadoPago Preapproval
    4. Saves mp_preapproval_id and status='pending' to DB
    5. Returns checkout_url for the tenant to approve the subscription

    billing_cycle must be 'monthly' or 'annual'.
    """
    if body.billing_cycle not in ("monthly", "annual"):
        from fastapi import HTTPException
        raise HTTPException(
            status_code=422,
            detail="billing_cycle debe ser 'monthly' o 'annual'",
        )

    session = require_valid_session(request)
    tenant_id = session.tenant_id

    async with get_db_connection() as conn:
        plan = await billing_service.get_plan_for_subscribe(conn, body.plan_id)
        tenant_email = await billing_service.get_tenant_email(conn, tenant_id)

        amount = (
            plan["price_monthly"]
            if body.billing_cycle == "monthly"
            else plan["price_annual"]
        )
        back_url = f"{settings.frontend_url}/billing/confirmacion"

        mp_result = await mercadopago_service.create_preapproval(
            plan_name=plan["name"],
            transaction_amount=amount,
            billing_cycle=body.billing_cycle,
            tenant_id=tenant_id,
            tenant_email=tenant_email or "",
            back_url=back_url,
        )

        return await billing_service.subscribe_tenant(
            conn,
            tenant_id=tenant_id,
            plan_id=body.plan_id,
            billing_cycle=body.billing_cycle,
            checkout_url=mp_result["checkout_url"],
            mp_preapproval_id=mp_result["mp_preapproval_id"],
        )


@tenant_router.get("/subscription")
async def get_my_subscription(request: Request):
    """Get the current subscription status for the authenticated tenant."""
    session = require_valid_session(request)
    async with get_db_connection(use_transaction=False) as conn:
        return await billing_service.get_tenant_subscription(conn, session.tenant_id)


@tenant_router.delete("/subscription")
async def cancel_my_subscription(request: Request):
    """
    Cancel the authenticated tenant's active subscription.
    Cancels both in the DB and in MercadoPago API.
    Returns 404 if there is no active/pending subscription.
    """
    session = require_valid_session(request)

    async with get_db_connection() as conn:
        mp_preapproval_id = await billing_service.cancel_tenant_subscription(
            conn, session.tenant_id
        )

    if mp_preapproval_id:
        await mercadopago_service.cancel_preapproval(mp_preapproval_id)

    return {"status": "cancelled", "mp_preapproval_id": mp_preapproval_id or None}


@tenant_router.post("/webhook", status_code=200)
async def mercadopago_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_signature: Optional[str] = Header(None, alias="x-signature"),
    x_request_id: Optional[str] = Header(None, alias="x-request-id"),
):
    """
    MercadoPago webhook endpoint — called directly by MP (no session auth).

    Verifies HMAC-SHA256 signature before processing.
    Handles all subscription and payment events:
    - subscription_preapproval → authorized  : activate subscription
    - subscription_preapproval → paused      : mark past_due + email
    - subscription_preapproval → cancelled   : log only (user already cancelled)
    - payment → approved                     : renew period + reset scan_usage + email
    - payment → rejected                     : mark past_due + email

    Emails are sent via BackgroundTasks so the 200 response is immediate.
    """
    query_params = dict(request.query_params)
    data_id = query_params.get("data.id") or query_params.get("id")

    if settings.mp_webhook_secret:
        is_valid = mercadopago_service.verify_webhook_signature(
            x_signature=x_signature,
            x_request_id=x_request_id,
            data_id=data_id,
            secret=settings.mp_webhook_secret,
        )
        if not is_valid:
            logger.warning("MP webhook: invalid signature — request rejected")
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    body = await request.json()
    event_type = body.get("type", "")
    action = body.get("action", "")
    resource_data = body.get("data", {})

    logger.info("MP webhook received: type=%s action=%s", event_type, action)

    if event_type == "subscription_preapproval":
        preapproval_id = resource_data.get("id") or data_id
        if not preapproval_id:
            return {"received": True}

        # Fetch current status from MP to be authoritative
        mp_status = await mercadopago_service.get_preapproval_status(preapproval_id)

        if mp_status == "authorized":
            async with get_db_connection() as conn:
                await billing_service.activate_tenant_subscription(conn, preapproval_id)

        elif mp_status == "paused":
            async with get_db_connection() as conn:
                tenant_info = await billing_service.mark_subscription_past_due(
                    conn, preapproval_id, "subscription_paused"
                )
            if tenant_info:
                background_tasks.add_task(
                    billing_email_service.send_payment_rejected_email,
                    tenant_name=tenant_info["tenant_name"],
                    tenant_email=tenant_info["tenant_email"],
                    billing_url=f"{settings.frontend_url}/billing",
                )

        elif mp_status in ("cancelled", "expired"):
            logger.info(
                "MP webhook: preapproval %s is %s — no action needed",
                preapproval_id, mp_status,
            )

    elif event_type == "payment":
        payment_id = resource_data.get("id") or data_id
        if not payment_id:
            return {"received": True}

        mp_payment = await mercadopago_service.get_payment_status(str(payment_id))
        payment_status = mp_payment["status"]
        external_reference = mp_payment.get("external_reference")  # tenant_id UUID string

        logger.info(
            "MP payment webhook: id=%s status=%s tenant=%s",
            payment_id, payment_status, external_reference,
        )

        if payment_status == "approved" and external_reference:
            async with get_db_connection() as conn:
                tenant_info = await billing_service.renew_subscription_period(
                    conn,
                    tenant_id_str=external_reference,
                    mp_payment_id=str(payment_id),
                    amount=mp_payment.get("transaction_amount", 0),
                    currency=mp_payment.get("currency_id", "COP"),
                )
            if tenant_info:
                background_tasks.add_task(
                    billing_email_service.send_payment_renewed_email,
                    tenant_name=tenant_info["tenant_name"],
                    tenant_email=tenant_info["tenant_email"],
                    next_period_end=tenant_info["next_period_end"],
                )

        elif payment_status in ("rejected", "cancelled") and external_reference:
            # Find preapproval_id via tenant subscription for mark_subscription_past_due
            async with get_db_connection() as conn:
                from uuid import UUID as _UUID
                try:
                    _tid = _UUID(external_reference)
                except ValueError:
                    logger.error("MP payment webhook: invalid external_reference=%s", external_reference)
                    return {"received": True}

                preapproval_row = await conn.fetchval(
                    "SELECT mp_preapproval_id FROM tenant_subscriptions WHERE tenant_id = $1",
                    _tid,
                )
                tenant_info = None
                if preapproval_row:
                    tenant_info = await billing_service.mark_subscription_past_due(
                        conn, preapproval_row, "payment_rejected"
                    )

            if tenant_info:
                background_tasks.add_task(
                    billing_email_service.send_payment_rejected_email,
                    tenant_name=tenant_info["tenant_name"],
                    tenant_email=tenant_info["tenant_email"],
                    billing_url=f"{settings.frontend_url}/billing",
                )

    return {"received": True}


# ── Grace period & access control — issue #62 ────────────────────────────────


@tenant_router.get("/access-status")
async def get_access_status(request: Request):
    """
    Return the subscription access level for the authenticated tenant.

    Levels:
      free             — no subscription; limited free plan
      full             — active or pending subscription
      full_with_warning — past_due, ≤ 3 days overdue
      read_only        — past_due, 3-7 days overdue
      blocked          — past_due > 7 days, or cancelled/expired
    """
    session = require_valid_session(request)
    async with get_db_connection(use_transaction=False) as conn:
        access = await billing_service.get_subscription_access(session.tenant_id, conn)
        return {
            "level": access.level,
            "grace_days_remaining": access.grace_days_remaining,
            "subscription_status": access.subscription_status,
            "next_payment_date": access.next_payment_date,
            "message": access.message,
        }


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

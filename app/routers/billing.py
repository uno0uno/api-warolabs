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
from app.services import billing_service, billing_email_service, billing_webhook_service, wompi_service

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


class GiftBody(BaseModel):
    """Body for POST /admin/billing/subscriptions/{tenant_id}/gift"""
    days: Optional[int] = None
    months: Optional[int] = None
    note: Optional[str] = None


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


@router.post("/subscriptions/{tenant_id}/gift", status_code=200)
async def gift_subscription(tenant_id: UUID, body: GiftBody, request: Request):
    """
    Extend (or create) a subscription for a tenant as a commercial gift.

    - days XOR months must be provided (not both, not neither).
    - If the tenant has no subscription, creates one (Plan Pro, annual cycle).
    - If the tenant already has an active subscription, extends current_period_end.
    - Always records a gift_granted billing event.
    """
    require_valid_session(request)
    if not body.days and not body.months:
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="Debe proveer 'days' o 'months'")
    if body.days and body.months:
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="Provee solo 'days' o 'months', no ambos")
    async with get_db_connection() as conn:
        return await billing_service.gift_tenant_subscription(
            conn,
            tenant_id=tenant_id,
            days=body.days,
            months=body.months,
            note=body.note,
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
    payer_email: Optional[str] = None


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
    Subscribe the authenticated tenant to a plan via Wompi Payment Link.

    1. Valida que el plan exista y esté activo
    2. Crea un Payment Link en Wompi
    3. Guarda wompi_link_id y status='pending' en DB
    4. Retorna checkout_url para redirigir al checkout de Wompi

    billing_cycle debe ser 'monthly' o 'annual'.
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

        amount = (
            plan["price_monthly"]
            if body.billing_cycle == "monthly"
            else plan["price_annual"]
        )

        # Strip any base path from frontend_url to get the root host
        # e.g. "http://localhost:8080/waro-colombia" → "http://localhost:8080"
        from urllib.parse import urlparse
        parsed = urlparse(settings.frontend_url)
        frontend_host = f"{parsed.scheme}://{parsed.netloc}"
        redirect_url = f"{frontend_host}/billing/confirmacion"

        wompi_result = await wompi_service.create_payment_link(
            plan_name=plan["name"],
            amount=amount,
            billing_cycle=body.billing_cycle,
            tenant_id=tenant_id,
            redirect_url=redirect_url,
        )

        return await billing_service.subscribe_tenant(
            conn,
            tenant_id=tenant_id,
            plan_id=body.plan_id,
            billing_cycle=body.billing_cycle,
            checkout_url=wompi_result["checkout_url"],
            gateway_reference=wompi_result["wompi_link_id"],
        )


@tenant_router.get("/subscription")
async def get_my_subscription(request: Request):
    """Get the current subscription status for the authenticated tenant."""
    session = require_valid_session(request)
    async with get_db_connection(use_transaction=False) as conn:
        return await billing_service.get_tenant_subscription(conn, session.tenant_id)


@tenant_router.get("/verify-payment")
async def verify_payment(request: Request, transaction_id: str = Query(...)):
    """
    Verifica el estado de una transacción de Wompi y activa la suscripción si fue aprobada.
    Llamado desde /billing/confirmacion al regresar del checkout de Wompi.
    """
    session = require_valid_session(request)
    tenant_id = session.tenant_id

    transaction = await wompi_service.get_transaction(transaction_id)
    wompi_status = transaction.get("status", "PENDING").upper()
    internal_status = wompi_service.map_status(wompi_status)
    payment_link_id = transaction.get("payment_link_id")

    async with get_db_connection() as conn:
        if internal_status == "active" and payment_link_id:
            await billing_service.activate_subscription_by_gateway_ref(
                conn,
                tenant_id=tenant_id,
                gateway_reference=payment_link_id,
                wompi_transaction_id=transaction_id,
                amount=transaction.get("amount_in_cents", 0) / 100,
            )

    return {
        "status": internal_status,
        "wompi_status": wompi_status,
        "transaction_id": transaction_id,
        "payment_link_id": payment_link_id,
    }


@tenant_router.get("/usage-history")
async def get_my_usage_history(
    request: Request,
    months: int = Query(12, ge=1, le=24, description="Número de meses a retornar"),
):
    """Monthly scan usage history for the authenticated tenant (last N months)."""
    session = require_valid_session(request)
    async with get_db_connection(use_transaction=False) as conn:
        return await billing_service.get_scan_monthly_history(session.tenant_id, conn, months)


@tenant_router.get("/events")
async def get_my_billing_events(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """Paginated billing events for the authenticated tenant (tenant_id from session cookie)."""
    session = require_valid_session(request)
    async with get_db_connection(use_transaction=False) as conn:
        return await billing_service.list_tenant_billing_events(conn, session.tenant_id, limit, offset)


@tenant_router.delete("/subscription")
async def cancel_my_subscription(request: Request):
    """
    Cancela la suscripción activa del tenant en la DB.
    Wompi Payment Links no requieren cancelación en la API.
    """
    session = require_valid_session(request)

    async with get_db_connection() as conn:
        gateway_reference = await billing_service.cancel_tenant_subscription(
            conn, session.tenant_id
        )

    return {"status": "cancelled", "gateway_reference": gateway_reference or None}


@tenant_router.post("/webhook", status_code=200)
async def wompi_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """
    Wompi webhook endpoint — llamado por Wompi al completar una transacción.

    Evento: transaction.updated
    - status APPROVED → activa la suscripción
    - status DECLINED/VOIDED/ERROR → marca como cancelled + email

    Verifica la firma incluida en el body del evento.
    """
    body = await request.json()
    event = body.get("event", "")

    logger.info("Wompi webhook received: event=%s", event)

    if not wompi_service.verify_event_signature(body):
        logger.warning("Wompi webhook: firma inválida — rechazando")
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    if event != "transaction.updated":
        return {"received": True}

    transaction = body.get("data", {}).get("transaction", {})
    wompi_status = transaction.get("status", "").upper()
    payment_link_id = transaction.get("payment_link_id", "")
    transaction_id = str(transaction.get("id", ""))
    amount_cents = transaction.get("amount_in_cents", 0)

    logger.info(
        "Wompi transaction: link=%s status=%s tx=%s",
        payment_link_id, wompi_status, transaction_id,
    )

    if not payment_link_id:
        return {"received": True}

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
            # Outgoing webhook (issue #156). No-op when BILLING_WEBHOOK_URL
            # is empty. Failures are logged but never block activation.
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

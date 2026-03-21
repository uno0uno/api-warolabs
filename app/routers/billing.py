"""
Billing Admin Router — issue #61

CRUD endpoints for subscription plans, tenant subscriptions, usage summary,
and billing events. All endpoints require a valid tenant session.
"""
from decimal import Decimal
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from app.core.middleware import require_valid_session
from app.database import get_db_connection
from app.services import billing_service

router = APIRouter(prefix="/admin/billing", tags=["Billing Admin"])


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

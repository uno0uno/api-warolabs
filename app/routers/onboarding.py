from typing import Optional
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, Body, Query, Request

from app.config import settings
from app.core.middleware import require_valid_session
from app.database import get_db_connection
from app.models.billing import (
    OnboardingCheckoutRequest,
    OnboardingCheckoutResponse,
    OnboardingPaymentStatusResponse,
    OnboardingPlansResponse,
)
from app.models.onboarding import (
    OnboardingBusinessProfileUpdate,
    OnboardingFinancialResponse,
    OnboardingStatusResponse,
)
from app.services import billing_service, wompi_service
from app.services.onboarding_service import (
    ensure_onboarding_payment_ready,
    get_onboarding_financial_profile,
    get_status_for_tenant,
    update_onboarding_financial_profile,
)


router = APIRouter(prefix="/onboarding", tags=["onboarding"])


@router.get("/status", response_model=OnboardingStatusResponse)
async def get_onboarding_status(request: Request):
    session = require_valid_session(request)
    async with get_db_connection(use_transaction=False) as conn:
        return await get_status_for_tenant(conn, session.tenant_id)


@router.get("/financial-profile", response_model=OnboardingFinancialResponse)
async def get_financial_profile(request: Request):
    session = require_valid_session(request)
    async with get_db_connection(use_transaction=False) as conn:
        return await get_onboarding_financial_profile(conn, session.tenant_id)


@router.put("/financial-profile", response_model=OnboardingFinancialResponse)
async def update_financial_profile(
    request: Request,
    profile_data: OnboardingBusinessProfileUpdate = Body(...),
):
    session = require_valid_session(request)
    async with get_db_connection() as conn:
        return await update_onboarding_financial_profile(
            conn, session.tenant_id, profile_data
        )


@router.get("/plans", response_model=OnboardingPlansResponse)
async def list_payment_plans(request: Request):
    session = require_valid_session(request)
    async with get_db_connection(use_transaction=False) as conn:
        await ensure_onboarding_payment_ready(conn, session)
        plans = await billing_service.list_onboarding_plans(conn)
    return {"success": True, "data": plans}


@router.post(
    "/checkout",
    response_model=OnboardingCheckoutResponse,
    status_code=201,
)
async def create_payment_checkout(
    request: Request,
    body: OnboardingCheckoutRequest = Body(...),
):
    session = require_valid_session(request)
    async with get_db_connection() as conn:
        await ensure_onboarding_payment_ready(conn, session)
        plan = await billing_service.get_plan_for_subscribe(conn, body.plan_id)
        attempt_id = await billing_service.create_onboarding_payment_attempt(
            conn,
            tenant_id=session.tenant_id,
            plan_id=body.plan_id,
            amount_in_cents=plan["amount_in_cents"],
            provider_environment=wompi_service.configured_event_environment(),
        )

    parsed = urlparse(settings.frontend_url)
    frontend_host = f"{parsed.scheme}://{parsed.netloc}"
    wompi_result = await wompi_service.create_payment_link(
        plan_name=plan["name"],
        amount_in_cents=plan["amount_in_cents"],
        billing_cycle="annual",
        sku=attempt_id,
        redirect_url=(
            f"{frontend_host}/billing/confirmacion?attempt_id={attempt_id}"
        ),
    )
    async with get_db_connection() as conn:
        await billing_service.attach_onboarding_payment_link(
            conn,
            attempt_id=attempt_id,
            tenant_id=session.tenant_id,
            provider_reference=wompi_result["wompi_link_id"],
            checkout_url=wompi_result["checkout_url"],
        )
    return {
        "attempt_id": attempt_id,
        "plan_id": body.plan_id,
        "checkout_url": wompi_result["checkout_url"],
        "amount_in_cents": plan["amount_in_cents"],
        "currency": "COP",
        "billing_cycle": "annual",
        "status": "pending",
    }


@router.get("/payment-status", response_model=OnboardingPaymentStatusResponse)
async def get_payment_status(
    request: Request,
    attempt_id: Optional[UUID] = Query(default=None),
):
    session = require_valid_session(request)
    async with get_db_connection(use_transaction=False) as conn:
        return await billing_service.get_onboarding_payment_attempt(
            conn,
            tenant_id=session.tenant_id,
            attempt_id=attempt_id,
        )

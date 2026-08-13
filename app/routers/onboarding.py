from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Query, Request

from app.core.middleware import require_valid_session
from app.database import get_db_connection
from app.models.billing import OnboardingPaymentStatusResponse
from app.models.onboarding import (
    AdditionalTenantBootstrapResponse,
    OnboardingBusinessProfileUpdate,
    OnboardingStatusResponse,
)
from app.services import billing_service
from app.services.onboarding_service import (
    bootstrap_additional_tenant,
    get_status_for_tenant,
)


router = APIRouter(prefix="/onboarding", tags=["onboarding"])


@router.get("/status", response_model=OnboardingStatusResponse)
async def get_onboarding_status(request: Request):
    session = require_valid_session(request)
    async with get_db_connection(use_transaction=False) as conn:
        return await get_status_for_tenant(conn, session.tenant_id)


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


@router.post(
    "/additional-tenant",
    response_model=AdditionalTenantBootstrapResponse,
)
async def create_additional_tenant(
    request: Request,
    payload: OnboardingBusinessProfileUpdate,
):
    """Create or resume another business for the logged-in superuser (no OTP)."""
    session = require_valid_session(request)
    async with get_db_connection() as conn:
        return await bootstrap_additional_tenant(conn, session, payload)

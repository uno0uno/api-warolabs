from fastapi import APIRouter, Body, Request

from app.core.middleware import require_valid_session
from app.database import get_db_connection
from app.models.onboarding import (
    OnboardingBusinessProfileUpdate,
    OnboardingFinancialResponse,
    OnboardingStatusResponse,
)
from app.services.onboarding_service import (
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

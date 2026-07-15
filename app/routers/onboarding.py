from fastapi import APIRouter, Request

from app.core.middleware import require_valid_session
from app.database import get_db_connection
from app.models.onboarding import OnboardingStatusResponse
from app.services.onboarding_service import get_status_for_tenant


router = APIRouter(prefix="/onboarding", tags=["onboarding"])


@router.get("/status", response_model=OnboardingStatusResponse)
async def get_onboarding_status(request: Request):
    session = require_valid_session(request)
    async with get_db_connection(use_transaction=False) as conn:
        return await get_status_for_tenant(conn, session.tenant_id)

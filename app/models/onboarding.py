from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field


TenantLifecycle = Literal["pending", "active", "suspended", "cancelled"]
OnboardingState = Literal[
    "email_verified",
    "business_profile_pending",
    "terms_pending",
    "payment_pending",
    "paid",
    "active",
    "setup_complete",
    "cancelled",
]


class OnboardingStatus(BaseModel):
    tenant_id: UUID = Field(alias="tenantId")
    lifecycle_status: TenantLifecycle = Field(alias="lifecycleStatus")
    state: Optional[OnboardingState] = None
    next_step: Optional[str] = Field(alias="nextStep", default=None)
    email_verified_at: Optional[datetime] = Field(alias="emailVerifiedAt", default=None)

    class Config:
        populate_by_name = True


class OnboardingStatusResponse(BaseModel):
    success: bool = True
    data: OnboardingStatus

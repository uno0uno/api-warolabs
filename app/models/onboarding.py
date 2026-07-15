from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.tenant_financial_profile import (
    CountryCurrencyOption,
    CurrencyMetadata,
    TenantFinancialProfile,
)


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
    financial_profile: Optional[TenantFinancialProfile] = Field(
        alias="financialProfile", default=None
    )
    terms_accepted: bool = Field(alias="termsAccepted", default=False)
    terms_version: Optional[str] = Field(alias="termsVersion", default=None)

    class Config:
        populate_by_name = True


class OnboardingStatusResponse(BaseModel):
    success: bool = True
    data: OnboardingStatus


class OnboardingFinancialData(BaseModel):
    profile: Optional[TenantFinancialProfile] = None
    catalog: list[CountryCurrencyOption]
    currencies: list[CurrencyMetadata]
    state: OnboardingState
    next_step: Optional[str] = Field(alias="nextStep", default=None)

    class Config:
        populate_by_name = True


class OnboardingFinancialResponse(BaseModel):
    success: bool = True
    data: OnboardingFinancialData

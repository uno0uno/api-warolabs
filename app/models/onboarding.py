from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.models.tenant_financial_profile import (
    CountryCurrencyOption,
    CurrencyMetadata,
    TenantFinancialProfile,
    TenantFinancialProfileUpdate,
)


TenantLifecycle = Literal["pending", "active", "suspended", "cancelled"]
OnboardingState = Literal[
    "email_verified",
    "business_profile_pending",
    "terms_pending",
    "starter_active",
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
    business_name: str = Field(alias="businessName")
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


class OnboardingBusinessProfileUpdate(TenantFinancialProfileUpdate):
    business_name: str = Field(alias="businessName", min_length=2, max_length=120)

    @field_validator("business_name", mode="before")
    @classmethod
    def _normalize_business_name(cls, value: str) -> str:
        normalized = " ".join(value.split()) if isinstance(value, str) else value
        if isinstance(normalized, str) and normalized.casefold() == "negocio pendiente":
            raise ValueError("Business name is required")
        return normalized

    class Config:
        populate_by_name = True


class OnboardingFinancialData(BaseModel):
    business_name: str = Field(alias="businessName")
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


class AdditionalTenantBootstrapData(BaseModel):
    """Authenticated second-business bootstrap (api-warolabs#834)."""

    tenant_id: UUID = Field(alias="tenantId")
    slug: str
    name: str
    resumed: bool
    lifecycle_status: TenantLifecycle = Field(alias="lifecycleStatus")
    state: Optional[OnboardingState] = None
    next_step: Optional[str] = Field(alias="nextStep", default=None)

    class Config:
        populate_by_name = True


class AdditionalTenantBootstrapResponse(BaseModel):
    success: bool = True
    data: AdditionalTenantBootstrapData

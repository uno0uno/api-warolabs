"""API models for the paid onboarding billing flow."""
from decimal import Decimal
from typing import Any, Dict, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class OnboardingPlan(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: UUID
    name: str
    slug: str
    description: Optional[str] = None
    price_annual: Decimal = Field(alias="priceAnnual")
    currency: Literal["COP"] = "COP"
    billing_cycle: Literal["annual"] = Field(default="annual", alias="billingCycle")
    features: Dict[str, Any] = Field(default_factory=dict)


class OnboardingPlansResponse(BaseModel):
    success: bool = True
    data: list[OnboardingPlan]


class OnboardingCheckoutRequest(BaseModel):
    plan_id: UUID


class OnboardingCheckoutResponse(BaseModel):
    attempt_id: UUID
    plan_id: UUID
    checkout_url: str
    amount_in_cents: int
    currency: Literal["COP"] = "COP"
    billing_cycle: Literal["annual"] = "annual"
    status: Literal["pending"] = "pending"


class OnboardingPaymentStatusResponse(BaseModel):
    attempt_id: UUID
    plan_id: UUID
    provider_reference: Optional[str] = None
    provider_transaction_id: Optional[str] = None
    amount_in_cents: int
    currency: Literal["COP"] = "COP"
    status: Literal["created", "pending", "approved", "declined", "error"]

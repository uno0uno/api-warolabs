"""Tenant financial country, base-currency and lock-state API models."""
from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, field_validator, model_validator

from app.core.tenant_prefs import validate_country_currency_pair


class TenantFinancialProfileUpdate(BaseModel):
    country_code: str
    base_currency_code: str

    @field_validator("country_code", "base_currency_code")
    @classmethod
    def _normalize_codes(cls, value: str) -> str:
        return value.strip().upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _validate_pair(self):
        self.country_code, self.base_currency_code = validate_country_currency_pair(
            self.country_code, self.base_currency_code
        )
        return self


class TenantFinancialProfile(BaseModel):
    tenant_id: UUID
    country_code: str
    base_currency_code: str
    accounting_localization: str
    document_mode: str
    fiscal_provider: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class CountryCurrencyOption(BaseModel):
    country_code: str
    currency_codes: list[str]


class CurrencyMetadata(BaseModel):
    currency_code: str
    minor_units: int


class FinancialCapabilities(BaseModel):
    colombia_puc: bool
    colombia_payroll: bool
    matias_dian: bool
    cop_wallet: bool
    wompi: bool
    fixed_cop_discounts: bool


class FinancialEligibility(BaseModel):
    eligible: bool
    lock_type: Literal["none", "temporary", "permanent"]
    reason_codes: list[str]


class TenantFinancialProfileResponse(BaseModel):
    profile: TenantFinancialProfile
    catalog: list[CountryCurrencyOption]
    currencies: list[CurrencyMetadata]
    capabilities: FinancialCapabilities
    eligibility: FinancialEligibility

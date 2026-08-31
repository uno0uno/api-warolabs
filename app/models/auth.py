import re

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator
from datetime import datetime
from typing import Literal, Optional, Dict, Any
from uuid import UUID

from app.core.email_utils import normalize_email
from app.core.tenant_prefs import (
    SUPPORTED_PHONE_COUNTRY_CODES,
    validate_country_currency_pair,
)
from app.models.onboarding import OnboardingStatus, OnboardingState, TenantLifecycle
from app.models.tenant_financial_profile import CountryCurrencyOption
from app.services.hospitality_tax_jurisdictions import (
    JURISDICTION_COUNTRIES,
    normalize_jurisdiction_code,
)

PreferredLocale = Literal['es', 'en', 'pt', 'fr', 'de', 'ar', 'hi', 'zh']
PosCatalogLayoutOverride = Literal['grid', 'list']


class User(BaseModel):
    id: UUID
    email: str
    name: Optional[str] = None
    created_at: datetime = Field(alias='createdAt')
    role: Optional[str] = None

    class Config:
        populate_by_name = True


class ProfileUser(User):
    user_name: Optional[str] = None
    description: Optional[str] = None
    logo_avatar: Optional[str] = None
    preferred_locale: Optional[PreferredLocale] = None
    pos_catalog_layout_override: Optional[PosCatalogLayoutOverride] = None


class Session(BaseModel):
    expires_at: datetime = Field(alias='expiresAt')
    created_at: datetime = Field(alias='createdAt')
    ip_address: Optional[str] = Field(alias='ipAddress', default=None)
    login_method: Optional[str] = Field(alias='loginMethod', default=None)
    tenant_id: Optional[UUID] = Field(alias='tenantId', default=None)
    
    class Config:
        populate_by_name = True

class Tenant(BaseModel):
    id: UUID
    name: str
    slug: str
    ui_locale: str = "es"

class SessionResponse(BaseModel):
    success: bool = True
    user: ProfileUser
    session: Session
    has_internal_access: bool = False
    current_tenant: Optional[Tenant] = Field(alias='currentTenant', default=None)
    lifecycle_status: TenantLifecycle = Field(alias='lifecycleStatus', default='active')
    onboarding_state: Optional[OnboardingState] = Field(alias='onboardingState', default=None)
    next_step: Optional[str] = Field(alias='nextStep', default=None)
    
    class Config:
        populate_by_name = True

# New models for magic link authentication
class MagicLinkRequest(BaseModel):
    email: str
    redirect: Optional[str] = None

    @field_validator("email", mode="before")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        return normalize_email(v)

class MagicLinkResponse(BaseModel):
    success: bool = True
    message: str = "Magic link sent successfully"
    action: Literal["email_sent", "registration_required"] = "email_sent"


class RegistrationMagicLinkResponse(BaseModel):
    success: bool = True
    action: Literal["verification_sent", "login_required"]


_ATTRIBUTION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


class RegistrationMagicLinkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    phone_country_code: int = Field(ge=1, le=999)
    phone_number: str
    consent: Literal[True]
    business_name: str = Field(min_length=2, max_length=120)
    country_code: str
    base_currency_code: str
    tax_jurisdiction_code: Optional[str] = None
    source: Optional[str] = None
    content: Optional[str] = None
    campaign: Optional[str] = None
    variant: Optional[str] = None
    visitor_key: Optional[str] = Field(default=None, max_length=128)

    @field_validator("email", mode="before")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return normalize_email(value)

    @field_validator("phone_number", mode="before")
    @classmethod
    def _normalize_phone(cls, value: str) -> str:
        digits = re.sub(r"\D", "", value or "")
        if len(digits) < 7 or len(digits) > 15:
            raise ValueError("Phone number must contain 7 to 15 digits")
        return digits

    @field_validator("phone_country_code")
    @classmethod
    def _validate_phone_country_code(cls, value: int) -> int:
        if value not in SUPPORTED_PHONE_COUNTRY_CODES:
            raise ValueError("Unsupported phone country calling code")
        return value

    @field_validator("business_name", mode="before")
    @classmethod
    def _normalize_business_name(cls, value: str) -> str:
        normalized = " ".join(value.split()) if isinstance(value, str) else value
        if isinstance(normalized, str) and normalized.casefold() == "negocio pendiente":
            raise ValueError("Business name is required")
        return normalized

    @field_validator("tax_jurisdiction_code", mode="before")
    @classmethod
    def _normalize_jurisdiction(cls, value: Optional[str]) -> Optional[str]:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value.strip().upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _validate_business_country_currency(self):
        self.country_code, self.base_currency_code = validate_country_currency_pair(
            self.country_code, self.base_currency_code
        )
        if self.country_code in JURISDICTION_COUNTRIES:
            if not self.tax_jurisdiction_code:
                raise ValueError("tax_jurisdiction_code is required for US and CA")
            try:
                self.tax_jurisdiction_code = normalize_jurisdiction_code(
                    self.country_code, self.tax_jurisdiction_code
                )
            except ValueError as exc:
                raise ValueError(str(exc)) from exc
        else:
            self.tax_jurisdiction_code = None
        return self

    @field_validator("source", "content", "campaign", "variant")
    @classmethod
    def _validate_attribution(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip()
        if not _ATTRIBUTION_PATTERN.fullmatch(normalized):
            raise ValueError("Attribution values must be slug-like and at most 100 characters")
        return normalized

    @field_validator("visitor_key", mode="before")
    @classmethod
    def _strip_visitor_key(cls, value: object) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str):
            text = value.strip()
            return text or None
        return None


class RegistrationVerifyTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=32, max_length=128, pattern=r"^[A-Fa-f0-9]+$")


class PhoneCountryOption(BaseModel):
    country_code: str
    calling_code: int


class RegistrationTaxJurisdictionOption(BaseModel):
    code: str
    label: str
    regime: str = ""
    rate: float = 0


class RegistrationOptionsResponse(BaseModel):
    catalog: list[CountryCurrencyOption]
    phone_countries: list[PhoneCountryOption]
    tax_jurisdictions: Dict[str, list[RegistrationTaxJurisdictionOption]] = Field(
        default_factory=dict
    )


class RegistrationVerifyCodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    code: str = Field(pattern=r"^[0-9]{6}$")

    @field_validator("email", mode="before")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return normalize_email(value)

class VerifyCodeRequest(BaseModel):
    email: str
    code: str

    @field_validator("email", mode="before")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        return normalize_email(v)

class VerifyTokenRequest(BaseModel):
    email: str
    token: str

    @field_validator("email", mode="before")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        return normalize_email(v)


class RegistrationAttribution(BaseModel):
    source: Optional[str] = None
    content: Optional[str] = None
    campaign: Optional[str] = None
    variant: Optional[str] = None


class VerifyCodeResponse(BaseModel):
    success: bool = True
    message: str = "Verification successful"
    user: User
    tenant: Tenant
    onboarding: Optional[OnboardingStatus] = None
    registration_attribution: Optional[RegistrationAttribution] = None

class VerifyTokenResponse(BaseModel):
    success: bool = True
    message: str = "Login successful"
    user: User
    tenant: Optional[Tenant] = None
    onboarding: Optional[OnboardingStatus] = None
    registration_attribution: Optional[RegistrationAttribution] = None

class UserTenantsResponse(BaseModel):
    success: bool = True
    data: list[Tenant]
    timestamp: Optional[str] = None

class SwitchTenantRequest(BaseModel):
    tenantSlug: str

class SwitchTenantResponse(BaseModel):
    success: bool = True
    message: str = "Tenant switched successfully"
    tenant: Tenant
    timestamp: Optional[str] = None


class UpdateProfileRequest(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    user_name: Optional[str] = None
    phone_number: Optional[str] = None
    city: Optional[str] = None
    description: Optional[str] = Field(default=None, max_length=500)
    preferred_locale: Optional[PreferredLocale] = None
    pos_catalog_layout_override: Optional[PosCatalogLayoutOverride] = None

    @field_validator('name', mode='before')
    @classmethod
    def _normalize_name(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError('Name cannot be empty')
        return normalized

    @field_validator('description', mode='before')
    @classmethod
    def _normalize_description(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator('pos_catalog_layout_override', mode='before')
    @classmethod
    def _normalize_pos_layout_override(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str):
            normalized = value.strip().lower()
            if not normalized:
                return None
            if normalized not in ('grid', 'list'):
                raise ValueError('pos_catalog_layout_override must be one of: grid, list')
            return normalized
        return value


class UpdateProfileResponse(BaseModel):
    success: bool = True
    message: str = "Profile updated successfully"
    user: ProfileUser


class ProfileAvatarResponse(BaseModel):
    success: bool = True
    url: str
    logo_avatar: str

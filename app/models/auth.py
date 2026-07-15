from pydantic import BaseModel, Field, field_validator
from datetime import datetime
from typing import Literal, Optional, Dict, Any
from uuid import UUID

from app.core.email_utils import normalize_email
from app.models.onboarding import OnboardingStatus, OnboardingState, TenantLifecycle

PreferredLocale = Literal['es', 'en', 'pt', 'fr', 'de', 'ar', 'hi', 'zh']


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

class VerifyCodeResponse(BaseModel):
    success: bool = True
    message: str = "Verification successful"
    user: User
    tenant: Tenant
    onboarding: Optional[OnboardingStatus] = None

class VerifyTokenResponse(BaseModel):
    success: bool = True
    message: str = "Login successful"
    user: User
    tenant: Optional[Tenant] = None
    onboarding: Optional[OnboardingStatus] = None

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


class UpdateProfileResponse(BaseModel):
    success: bool = True
    message: str = "Profile updated successfully"
    user: ProfileUser


class ProfileAvatarResponse(BaseModel):
    success: bool = True
    url: str
    logo_avatar: str

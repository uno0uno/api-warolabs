# Tenant Public Profile models for restaurant public pages
from pydantic import BaseModel, Field, field_validator
from typing import Optional, Dict, Any
from uuid import UUID
from datetime import datetime
from decimal import Decimal

class BusinessHours(BaseModel):
    """Business hours for a single day"""
    open: Optional[str] = Field(None, description="Opening time (HH:MM format)")
    close: Optional[str] = Field(None, description="Closing time (HH:MM format)")
    closed: bool = Field(False, description="Whether the business is closed this day")

class SocialMedia(BaseModel):
    """Social media links"""
    facebook: Optional[str] = None
    instagram: Optional[str] = None
    whatsapp: Optional[str] = None
    twitter: Optional[str] = None
    tiktok: Optional[str] = None

class TenantPublicProfileBase(BaseModel):
    """Base tenant public profile fields"""
    slug: str = Field(..., min_length=1, max_length=255, description="URL-friendly slug (e.g., 'la-hamburgueseria')")
    display_name: str = Field(..., min_length=1, max_length=255, description="Public display name")
    description: Optional[str] = Field(None, description="Restaurant description")
    logo_url: Optional[str] = Field(None, max_length=500, description="Logo URL")
    banner_url: Optional[str] = Field(None, max_length=500, description="Banner/hero image URL")

    # Contact info
    phone_number: Optional[str] = Field(None, max_length=50, description="Public phone number")
    email: Optional[str] = Field(None, max_length=255, description="Contact email")
    address: Optional[str] = Field(None, max_length=500, description="Physical address")

    # Location
    city: Optional[str] = Field(None, max_length=255)
    neighborhood: Optional[str] = Field(None, max_length=255)
    latitude: Optional[Decimal] = Field(None, description="Latitude coordinate")
    longitude: Optional[Decimal] = Field(None, description="Longitude coordinate")

    # Business hours (JSONB)
    business_hours: Optional[Dict[str, Any]] = Field(
        None,
        description="Business hours by day: {monday: {open: '09:00', close: '22:00', closed: false}, ...}"
    )

    # Social media (JSONB)
    social_media: Optional[Dict[str, str]] = Field(
        None,
        description="Social media links: {facebook: 'url', instagram: '@handle', whatsapp: '+57...', ...}"
    )

    # SEO
    seo_title: Optional[str] = Field(None, max_length=255, description="Meta title for SEO")
    seo_description: Optional[str] = Field(None, description="Meta description for SEO")

    # Online ordering gate — controls storefront catalog ordering AND POS delivery toggle
    accepts_online_orders: bool = Field(False, description="Whether the tenant accepts online orders. Gates storefront catalog ordering AND the POS delivery toggle.")
    min_order_amount: Decimal = Field(Decimal('0'), description="Minimum order amount (future)")
    estimated_preparation_time: int = Field(30, description="Estimated preparation time in minutes")

    # Manual open/close toggle (operator override)
    is_manually_open: bool = Field(True, description="Operator manual toggle: False = closed regardless of business_hours")

    # Table management module flag
    tables_enabled: bool = Field(False, description="Whether the table management module is enabled for this tenant")

    # KDS / Comandas module flags
    comandas_enabled: bool = Field(False, description="Whether the comandas/KDS module is enabled. When false, system behaves exactly as today.")
    kds_enabled: bool = Field(False, description="Whether KDS station screens (/cocina/[id]) are enabled. Requires comandas_enabled=true.")

    # POS personalization (issue #529)
    auto_select_generic_enabled: bool = Field(
        False,
        description="POS: when true, /pos/checkout pre-selects the Genérico customer (phone_number='0000000000') in counter/bar mode."
    )

    # POS expediter mode (issue #537)
    expediter_enabled: bool = Field(
        False,
        description="POS expediter: when true, waiters can advance comanda state "
                    "(preparing → ready → delivered) from /pos via a slide-over panel, "
                    "without touching the KDS. Requires comandas_enabled=true."
    )

class TenantPublicProfileCreate(TenantPublicProfileBase):
    """Create tenant public profile"""
    tenant_id: UUID = Field(..., description="Tenant ID")
    is_active: bool = Field(False, description="Whether the public profile is active")

class TenantPublicProfileUpdate(BaseModel):
    """Update tenant public profile (all fields optional)"""
    slug: Optional[str] = Field(None, min_length=1, max_length=255)
    display_name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    logo_url: Optional[str] = Field(None, max_length=500)
    banner_url: Optional[str] = Field(None, max_length=500)

    phone_number: Optional[str] = Field(None, max_length=50)
    email: Optional[str] = Field(None, max_length=255)
    address: Optional[str] = Field(None, max_length=500)

    city: Optional[str] = Field(None, max_length=255)
    neighborhood: Optional[str] = Field(None, max_length=255)
    latitude: Optional[Decimal] = None
    longitude: Optional[Decimal] = None

    business_hours: Optional[Dict[str, Any]] = None
    social_media: Optional[Dict[str, str]] = None

    seo_title: Optional[str] = Field(None, max_length=255)
    seo_description: Optional[str] = None

    is_active: Optional[bool] = None
    accepts_online_orders: Optional[bool] = None
    min_order_amount: Optional[Decimal] = None
    estimated_preparation_time: Optional[int] = None
    is_manually_open: Optional[bool] = None
    tables_enabled: Optional[bool] = None
    comandas_enabled: Optional[bool] = None
    kds_enabled: Optional[bool] = None
    auto_select_generic_enabled: Optional[bool] = None
    expediter_enabled: Optional[bool] = None

class TenantPublicProfile(TenantPublicProfileBase):
    """Complete tenant public profile with all fields"""
    id: UUID
    tenant_id: UUID
    is_active: bool
    created_at: datetime
    updated_at: datetime

    # Calculated fields
    is_currently_open: Optional[bool] = Field(None, description="Calculated: is restaurant currently open")

    class Config:
        from_attributes = True

class TenantPublicProfileResponse(BaseModel):
    """Single tenant public profile response"""
    success: bool = True
    data: TenantPublicProfile

class TenantPublicProfilesListResponse(BaseModel):
    """List of tenant public profiles response"""
    success: bool = True
    total: int
    data: list[TenantPublicProfile]

class ToggleProfileRequest(BaseModel):
    """Request to activate/deactivate public profile"""
    is_active: bool = Field(..., description="Whether to activate or deactivate the profile")

class ToggleProfileResponse(BaseModel):
    """Response after toggling profile activation"""
    success: bool = True
    message: str
    is_active: bool

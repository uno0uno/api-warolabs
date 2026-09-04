# Tenant Public Profile models for restaurant public pages
from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Optional, Dict, Any
from uuid import UUID
from datetime import datetime
from decimal import Decimal
from app.core.timezones import DEFAULT_TENANT_TIMEZONE, validate_timezone
from app.core.tenant_prefs import (
    DEFAULT_CURRENCY_CODE,
    DEFAULT_TENANT_LOCALE,
    validate_currency_code,
    validate_locale,
)
from app.models.tenant_financial_profile import TenantFinancialProfileResponse

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
    country: Optional[str] = Field(
        'Colombia', max_length=80,
        description="Country (warocol.com#615). v1 locks the UI to Colombia.",
    )
    city: Optional[str] = Field(None, max_length=255)
    city_slug: Optional[str] = Field(
        None, max_length=120,
        description="Normalized slug for the city directory (warocol.com#615). "
                    "Must match an active public_cities entry.",
    )
    neighborhood: Optional[str] = Field(None, max_length=255)
    latitude: Optional[Decimal] = Field(None, description="Latitude coordinate")
    longitude: Optional[Decimal] = Field(None, description="Longitude coordinate")
    timezone: str = Field(
        DEFAULT_TENANT_TIMEZONE,
        description="IANA timezone for tenant operational dates and business hours",
    )
    locale: str = Field(
        DEFAULT_TENANT_LOCALE,
        description="UI/number language preference: es | en. Defaults to es.",
    )
    currency_code: str = Field(
        DEFAULT_CURRENCY_CODE,
        description="ISO 4217 display currency code. Defaults to COP. Display-only; no FX.",
    )

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
    online_order_max_amount: Optional[Decimal] = Field(None, ge=0, description="Maximum online order amount for customer validation. NULL keeps tier defaults; 0 disables amount limit.")
    estimated_preparation_time: int = Field(30, description="Estimated preparation time in minutes")

    # Manual open/close toggle (operator override)
    is_manually_open: bool = Field(True, description="Operator manual toggle: False = closed regardless of business_hours")

    # Table management module flag
    tables_enabled: bool = Field(False, description="Whether the table management module is enabled for this tenant")

    # Table QR ordering (warocol.com#710)
    table_qr_module_enabled: bool = Field(
        False,
        description="When true, tenant can enable per-table static QR links for "
                    "diner self-order with staff confirmation in Despacho.",
    )

    # KDS / Comandas module flags
    comandas_enabled: bool = Field(False, description="Whether the comandas/KDS module is enabled. When false, system behaves exactly as today.")
    kds_enabled: bool = Field(False, description="Whether KDS station screens (/cocina/[id]) are enabled. Requires comandas_enabled=true.")

    # Internal Kali assistant feature flag. This is intentionally controlled
    # by SQL/config only, not exposed through tenant-facing toggle endpoints.
    kali_enabled: bool = Field(
        False,
        description="Internal-only flag: whether the Kali AI assistant is enabled for this tenant.",
    )

    # POS personalization (issue #529)
    auto_select_generic_enabled: bool = Field(
        False,
        description="POS: when true, /pos/checkout pre-selects the Genérico customer (phone_number='0000000000') in counter/bar mode."
    )

    open_sale_enabled: bool = Field(
        False,
        description="When true, POS shows Venta libre and the tenant shell open-priced product is active.",
    )

    # POS expediter mode (issue #537)
    expediter_enabled: bool = Field(
        False,
        description="POS expediter: when true, waiters can advance comanda state "
                    "(preparing → ready → delivered) from /pos via a slide-over panel, "
                    "without touching the KDS. Requires comandas_enabled=true."
    )

    # Minimum consumption / cover config (warocol.com#1368).
    # This batch only persists tenant config. Later batches snapshot/apply it to
    # sessions, deposits, close enforcement, accounting and reports.
    minimum_consumption_enabled: bool = Field(
        False,
        description="When true, table sessions may use tenant minimum consumption / cover rules.",
    )
    minimum_consumption_amount: Decimal = Field(
        Decimal('0'),
        ge=0,
        description="Minimum consumption / cover amount in COP for table sessions.",
    )
    minimum_consumption_restrictive: bool = Field(
        False,
        description="When true, later close-session logic may block closing below the minimum.",
    )

    # Waiter attribution family feature flag (warocol.com#573)
    waiter_attribution_enabled: bool = Field(
        False,
        description="When true, surfaces the waiter assignment family of features: "
                    "admin panel in /operaciones/comandas (#573), POS mesa override "
                    "(#574), and bar/counter order attribution (#575). Independent "
                    "of tables_enabled — bar/counter modes work without tables."
    )

    # Custom mesa label (warocol.com#614) — tenant-global override for the noun
    # used across the UI. NULL means the frontend falls back to defaults
    # ("Mesa" / "Mesas"). Empty/whitespace input on the API normalizes to NULL.
    tables_label_singular: Optional[str] = Field(
        None,
        min_length=1,
        max_length=40,
        description="Custom singular noun for 'Mesa' (e.g. 'Habitación' for hotels). NULL = use default.",
    )
    tables_label_plural: Optional[str] = Field(
        None,
        min_length=1,
        max_length=40,
        description="Custom plural noun for 'Mesas' (e.g. 'Habitaciones'). NULL = use default.",
    )

    # Tipping configuration (warocol.com#635) — phase 1 direct attribution.
    # tip_enabled gates the checkout selector and the /ventas/propinas view.
    # Defaults preserve current behaviour (tipping hidden).
    tip_enabled: bool = Field(
        False,
        description="Master tipping toggle (warocol.com#635). When true, "
                    "surfaces the tip selector at POS/online checkout and the "
                    "/ventas/propinas history view.",
    )
    tip_taxable_default: bool = Field(
        False,
        description="warocol.com#740 — default: apply consumption tax to tips "
                    "at checkout (gravada). Cashier may override per sale.",
    )
    tip_default_percentages: list[Decimal] = Field(
        default_factory=lambda: [Decimal('10')],
        description="Suggested tip presets shown as chips at checkout "
                    "(warocol.com#635). Resolved on subtotal (pre-tax). Max 5 "
                    "entries, each between 0 and 100.",
    )
    tip_preselect_index: Optional[int] = Field(
        None,
        description="Index into tip_default_percentages to pre-select at "
                    "checkout. NULL = nothing pre-selected (Ley 1935 voluntariness).",
    )

    # POS catalog presentation defaults (warocol.com#2495)
    pos_catalog_layout_default: str = Field(
        "grid",
        description="Default POS catalog layout: grid | list.",
    )
    pos_show_product_image: bool = Field(
        True,
        description="When true, POS catalog shows product images.",
    )
    pos_show_search: bool = Field(
        True,
        description="When true, POS catalog shows the search bar.",
    )
    deduct_inventory_on_command: bool = Field(
        False,
        description=(
            "When true, inventory qty is deducted when items are sent to kitchen "
            "(mesa/tab command) or on QR/delivery accept. COGS GL still posts at checkout. "
            "Default false (opt-in)."
        ),
    )
    hide_products_without_stock: bool = Field(
        False,
        description=(
            "When true, selling catalogs hide products that have a recipe but cannot "
            "make qty>=1 from current tenant_inventory. Products without recipes stay "
            "visible. Default false (opt-in). warocol.com#2574"
        ),
    )

    @field_validator('timezone')
    @classmethod
    def _validate_timezone(cls, v):
        return validate_timezone(v)

    @field_validator('locale')
    @classmethod
    def _validate_locale(cls, v):
        return validate_locale(v)

    @field_validator('currency_code')
    @classmethod
    def _validate_currency_code(cls, v):
        return validate_currency_code(v)

    @field_validator('tip_default_percentages')
    @classmethod
    def _validate_tip_presets(cls, v):
        # Mirror DB CHECK constraints from migration 078 + the per-issue rule:
        # max 5 entries, each 0 <= p <= 100. Empty list is rejected — if the
        # tenant wants no presets, they should set tip_enabled = false instead.
        if v is None:
            return v
        if len(v) == 0:
            raise ValueError("tip_default_percentages must contain at least one preset")
        if len(v) > 5:
            raise ValueError("tip_default_percentages cannot contain more than 5 presets")
        for p in v:
            if p < 0 or p > 100:
                raise ValueError(f"tip preset {p} must be between 0 and 100")
        return v

    @model_validator(mode='after')
    def _validate_tip_preselect_index(self):
        # Only runs when both fields are present on the model instance. For
        # partial-update payloads (Update model) the validator on the Update
        # class enforces the same rule scoped to fields sent in the request.
        if self.tip_preselect_index is None:
            return self
        if self.tip_preselect_index < 0:
            raise ValueError("tip_preselect_index must be non-negative")
        if self.tip_default_percentages is None:
            raise ValueError("tip_preselect_index requires tip_default_percentages to be set")
        if self.tip_preselect_index >= len(self.tip_default_percentages):
            raise ValueError(
                f"tip_preselect_index {self.tip_preselect_index} is out of bounds for "
                f"tip_default_percentages of length {len(self.tip_default_percentages)}"
            )
        return self

class TenantPublicProfileCreate(TenantPublicProfileBase):
    """Create tenant public profile"""
    tenant_id: UUID = Field(..., description="Tenant ID")
    is_active: bool = Field(False, description="Whether the public profile is active")

    @model_validator(mode='after')
    def _financial_fields_are_not_mutable_here(self):
        if self.currency_code != DEFAULT_CURRENCY_CODE or self.country not in (None, 'Colombia'):
            raise ValueError(
                "country and currency_code are read-only here; use /financial-profile"
            )
        return self

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

    country: Optional[str] = Field(None, max_length=80)
    city: Optional[str] = Field(None, max_length=255)
    city_slug: Optional[str] = Field(None, max_length=120)
    neighborhood: Optional[str] = Field(None, max_length=255)
    latitude: Optional[Decimal] = None
    longitude: Optional[Decimal] = None
    timezone: Optional[str] = None
    locale: Optional[str] = None
    currency_code: Optional[str] = None

    business_hours: Optional[Dict[str, Any]] = None
    social_media: Optional[Dict[str, str]] = None

    seo_title: Optional[str] = Field(None, max_length=255)
    seo_description: Optional[str] = None

    is_active: Optional[bool] = None
    accepts_online_orders: Optional[bool] = None
    min_order_amount: Optional[Decimal] = None
    online_order_max_amount: Optional[Decimal] = Field(None, ge=0)
    estimated_preparation_time: Optional[int] = None
    is_manually_open: Optional[bool] = None
    tables_enabled: Optional[bool] = None
    table_qr_module_enabled: Optional[bool] = None
    comandas_enabled: Optional[bool] = None
    kds_enabled: Optional[bool] = None
    auto_select_generic_enabled: Optional[bool] = None
    expediter_enabled: Optional[bool] = None
    minimum_consumption_enabled: Optional[bool] = None
    minimum_consumption_amount: Optional[Decimal] = Field(None, ge=0)
    minimum_consumption_restrictive: Optional[bool] = None
    waiter_attribution_enabled: Optional[bool] = None

    # Custom mesa label (warocol.com#614)
    tables_label_singular: Optional[str] = Field(None, min_length=1, max_length=40)
    tables_label_plural: Optional[str] = Field(None, min_length=1, max_length=40)

    # Tipping configuration (warocol.com#635)
    tip_enabled: Optional[bool] = None
    tip_taxable_default: Optional[bool] = None
    tip_default_percentages: Optional[list[Decimal]] = None
    tip_preselect_index: Optional[int] = None

    # POS catalog presentation defaults (warocol.com#2495)
    pos_catalog_layout_default: Optional[str] = None
    pos_show_product_image: Optional[bool] = None
    pos_show_search: Optional[bool] = None
    # warocol.com#2566
    deduct_inventory_on_command: Optional[bool] = None
    # warocol.com#2574
    hide_products_without_stock: Optional[bool] = None

    @field_validator('timezone')
    @classmethod
    def _validate_timezone_update(cls, v):
        if v is None:
            return v
        return validate_timezone(v)

    @field_validator('locale')
    @classmethod
    def _validate_locale_update(cls, v):
        if v is None:
            return v
        return validate_locale(v)

    @field_validator('currency_code')
    @classmethod
    def _validate_currency_code_update(cls, v):
        if v is None:
            return v
        return validate_currency_code(v)

    @field_validator('tip_default_percentages')
    @classmethod
    def _validate_tip_presets_update(cls, v):
        # Same rules as the Base validator; runs only when the field is sent.
        if v is None:
            return v
        if len(v) == 0:
            raise ValueError("tip_default_percentages must contain at least one preset")
        if len(v) > 5:
            raise ValueError("tip_default_percentages cannot contain more than 5 presets")
        for p in v:
            if p < 0 or p > 100:
                raise ValueError(f"tip preset {p} must be between 0 and 100")
        return v

    @model_validator(mode='after')
    def _validate_tip_preselect_index_update(self):
        # Only enforces the bounds rule when both fields are present in the
        # SAME payload. If the operator PATCHes only tip_preselect_index, we
        # cannot validate against the existing array without a DB read — let
        # the service guard against that case if needed. Negative is always
        # rejected since it's nonsense.
        if self.tip_preselect_index is None:
            return self
        if self.tip_preselect_index < 0:
            raise ValueError("tip_preselect_index must be non-negative")
        if self.tip_default_percentages is not None:
            if self.tip_preselect_index >= len(self.tip_default_percentages):
                raise ValueError(
                    f"tip_preselect_index {self.tip_preselect_index} is out of bounds for "
                    f"tip_default_percentages of length {len(self.tip_default_percentages)}"
                )
        return self

    @model_validator(mode='after')
    def _financial_fields_are_not_mutable_here(self):
        if self.country is not None or self.currency_code is not None:
            raise ValueError(
                "country and currency_code are read-only here; use /financial-profile"
            )
        return self

class TenantPublicProfile(TenantPublicProfileBase):
    """Complete tenant public profile with all fields"""
    id: UUID
    tenant_id: UUID
    is_active: bool
    created_at: datetime
    updated_at: datetime

    # Calculated fields
    is_currently_open: Optional[bool] = Field(None, description="Calculated: is restaurant currently open")
    country_code: Optional[str] = Field(
        None, description="Authoritative ISO country code from the financial profile"
    )
    base_currency_code: Optional[str] = Field(
        None, description="Authoritative tenant base currency"
    )

    class Config:
        from_attributes = True

class TenantPublicProfileResponse(BaseModel):
    """Single tenant public profile response"""
    success: bool = True
    data: TenantPublicProfile
    financial: Optional[TenantFinancialProfileResponse] = None

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

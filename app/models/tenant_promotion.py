"""Pydantic models for tenant promotions (warocol.com#980)."""
from datetime import datetime, time
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class PromoType(str, Enum):
    PERCENT_OFF = "percent_off"
    FIXED_OFF = "fixed_off"
    BOGO = "bogo"


class ScopeType(str, Enum):
    ALL_PRODUCTS = "all_products"
    CATEGORIES = "categories"
    PRODUCTS = "products"


def _validate_schedule_window(
    *,
    start_time: time,
    end_time: time,
    crosses_midnight: bool,
) -> None:
    if not crosses_midnight and end_time <= start_time:
        raise ValueError(
            "end_time must be after start_time when crosses_midnight is false"
        )


class PromotionScheduleInput(BaseModel):
    days_of_week: int = Field(
        ...,
        ge=1,
        le=127,
        description="Bitmask Mon=1, Tue=2, Wed=4, Thu=8, Fri=16, Sat=32, Sun=64",
    )
    start_time: time
    end_time: time
    crosses_midnight: bool = False
    sort_order: int = Field(0, ge=0, le=32767)

    @model_validator(mode="after")
    def _validate_window(self):
        _validate_schedule_window(
            start_time=self.start_time,
            end_time=self.end_time,
            crosses_midnight=self.crosses_midnight,
        )
        return self


class PromotionScheduleResponse(PromotionScheduleInput):
    id: UUID


class PromotionCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    promo_type: PromoType
    value_json: Dict[str, Any] = Field(default_factory=dict)
    scope_type: ScopeType
    category_ids: List[UUID] = Field(default_factory=list)
    product_ids: List[UUID] = Field(default_factory=list)
    schedules: List[PromotionScheduleInput] = Field(default_factory=list)
    priority: int = Field(0, ge=0, le=32767)
    is_active: bool = True
    stackable: bool = False
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    overlap_acknowledged: bool = False

    @model_validator(mode="after")
    def _validate_scope_and_values(self):
        if self.scope_type == ScopeType.CATEGORIES and not self.category_ids:
            raise ValueError("category_ids required when scope_type is categories")
        if self.scope_type == ScopeType.PRODUCTS and not self.product_ids:
            raise ValueError("product_ids required when scope_type is products")
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        if self.promo_type == PromoType.PERCENT_OFF:
            pct = self.value_json.get("percent")
            if pct is None or not (0 < float(pct) <= 100):
                raise ValueError("percent_off requires value_json.percent in (0, 100]")
        elif self.promo_type == PromoType.FIXED_OFF:
            if self.value_json.get("amount_cop") is None:
                raise ValueError("fixed_off requires value_json.amount_cop")
        elif self.promo_type == PromoType.BOGO:
            buy = self.value_json.get("buy_qty")
            get = self.value_json.get("get_qty")
            if not buy or not get or int(buy) < 1 or int(get) < 1:
                raise ValueError("bogo requires value_json.buy_qty and get_qty >= 1")
        return self


class PromotionUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    promo_type: Optional[PromoType] = None
    value_json: Optional[Dict[str, Any]] = None
    scope_type: Optional[ScopeType] = None
    category_ids: Optional[List[UUID]] = None
    product_ids: Optional[List[UUID]] = None
    schedules: Optional[List[PromotionScheduleInput]] = None
    priority: Optional[int] = Field(None, ge=0, le=32767)
    is_active: Optional[bool] = None
    stackable: Optional[bool] = None
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    overlap_acknowledged: bool = False


class PromotionResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    name: str
    promo_type: PromoType
    value_json: Dict[str, Any]
    scope_type: ScopeType
    category_ids: List[UUID]
    product_ids: List[UUID]
    schedules: List[PromotionScheduleResponse]
    priority: int
    is_active: bool
    stackable: bool
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    is_currently_active: Optional[bool] = None
    created_at: datetime
    updated_at: datetime


class PromotionListResponse(BaseModel):
    success: bool = True
    total: int
    data: List[PromotionResponse]


class PromotionSingleResponse(BaseModel):
    success: bool = True
    data: PromotionResponse

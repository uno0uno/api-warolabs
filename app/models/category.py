# Category models
from pydantic import BaseModel, Field, field_validator
from typing import Optional, List
from uuid import UUID
from datetime import datetime
import re

_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def normalize_category_color(value: Optional[str]) -> Optional[str]:
    """Normalize #RRGGBB or empty → None. Raises ValueError if invalid."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if not raw.startswith("#") and len(raw) == 6:
        raw = f"#{raw}"
    if not _HEX_COLOR_RE.match(raw):
        raise ValueError("color must be #RRGGBB")
    return raw.upper()


class CategoryBase(BaseModel):
    """Base category fields"""
    name: str
    description: Optional[str] = None
    color: Optional[str] = Field(
        default=None,
        description="POS card color #RRGGBB; null uses POS keyword/neutral fallback",
    )

    @field_validator("color", mode="before")
    @classmethod
    def _validate_color(cls, value: Optional[str]) -> Optional[str]:
        try:
            return normalize_category_color(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


class Category(CategoryBase):
    """Complete category. tenant_id is None for global categories
    (visible to every tenant) and a UUID for per-tenant categories
    (visible only to that tenant)."""
    id: UUID
    tenant_id: Optional[UUID] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class CategoryCreate(BaseModel):
    """Payload for POST /menu/categories. The tenant_id is taken from
    the authenticated session, not from the request body."""
    name: str = Field(..., min_length=1, max_length=100, description="Category display name")
    description: Optional[str] = Field(None, max_length=500, description="Optional descriptive text")
    color: Optional[str] = Field(
        None,
        description="Optional POS card color #RRGGBB",
    )

    @field_validator("color", mode="before")
    @classmethod
    def _validate_color(cls, value: Optional[str]) -> Optional[str]:
        try:
            return normalize_category_color(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


class CategoryUpdate(BaseModel):
    """Payload for PUT /menu/categories/{id}. Fields are optional —
    only the provided ones are updated. tenant_id is immutable and
    taken from the session. Send color: null to clear."""
    name: Optional[str] = Field(None, min_length=1, max_length=100, description="New display name")
    description: Optional[str] = Field(None, max_length=500, description="New description")
    color: Optional[str] = Field(
        None,
        description="POS card color #RRGGBB; null clears",
    )

    @field_validator("color", mode="before")
    @classmethod
    def _validate_color(cls, value: Optional[str]) -> Optional[str]:
        try:
            return normalize_category_color(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


class CategoriesListResponse(BaseModel):
    """List of categories response"""
    success: bool = True
    total: int
    data: List[Category]


class CategoryResponse(BaseModel):
    """Single category response (for POST)"""
    success: bool = True
    data: Category


class ReorderOnlineMenuCategoriesRequest(BaseModel):
    """Payload for PATCH /menu/categories/online-menu/reorder."""
    category_ids: List[UUID] = Field(..., min_length=1)


class ReorderOnlineMenuProductsRequest(BaseModel):
    """Payload for PATCH /menu/categories/online-menu/products/reorder."""
    category_id: UUID
    product_ids: List[UUID] = Field(..., min_length=1)


class OnlineMenuProductSummary(BaseModel):
    """Product row for Negocio online-menu product ordering."""
    id: UUID
    name: str
    category_id: UUID
    is_available_online: bool = False
    is_available_table_qr: bool = False


class OnlineMenuProductsListResponse(BaseModel):
    success: bool = True
    total: int
    data: List[OnlineMenuProductSummary]

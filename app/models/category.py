# Category models
from pydantic import BaseModel, Field
from typing import Optional, List
from uuid import UUID
from datetime import datetime


class CategoryBase(BaseModel):
    """Base category fields"""
    name: str
    description: Optional[str] = None


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


class CategoryUpdate(BaseModel):
    """Payload for PUT /menu/categories/{id}. Fields are optional —
    only the provided ones are updated. tenant_id is immutable and
    taken from the session."""
    name: Optional[str] = Field(None, min_length=1, max_length=100, description="New display name")
    description: Optional[str] = Field(None, max_length=500, description="New description")


class CategoriesListResponse(BaseModel):
    """List of categories response"""
    success: bool = True
    total: int
    data: List[Category]


class CategoryResponse(BaseModel):
    """Single category response (for POST)"""
    success: bool = True
    data: Category

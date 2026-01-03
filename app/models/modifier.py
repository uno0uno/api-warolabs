# Modifier models for menu management
from pydantic import BaseModel, Field
from typing import Optional, List
from uuid import UUID
from datetime import datetime
from decimal import Decimal


class ProductInfo(BaseModel):
    """Basic product info for modifier group association"""
    id: UUID
    name: str


class ModifierBase(BaseModel):
    """Base modifier fields"""
    name: str = Field(..., min_length=1, max_length=255, description="Modifier name")
    price: Decimal = Field(default=0, description="Additional price (can be negative)")
    max_limit: int = Field(default=1, ge=1, description="Maximum quantity allowed")
    is_default: bool = Field(default=False, description="Selected by default")
    is_available: bool = Field(default=True, description="Available for selection")
    sort_order: int = Field(default=0, ge=0, description="Display order")

class ModifierCreate(ModifierBase):
    """Create modifier"""
    pass

class ModifierUpdate(BaseModel):
    """Update modifier fields"""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    price: Optional[Decimal] = None
    max_limit: Optional[int] = Field(None, ge=1)
    is_default: Optional[bool] = None
    is_available: Optional[bool] = None
    sort_order: Optional[int] = Field(None, ge=0)

class Modifier(ModifierBase):
    """Complete modifier"""
    id: UUID
    modifier_group_id: UUID
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

class ModifierGroupBase(BaseModel):
    """Base modifier group fields"""
    name: str = Field(..., min_length=1, max_length=255, description="Group name")
    min_qty: int = Field(default=0, ge=0, description="Minimum selection quantity")
    max_qty: int = Field(default=1, ge=1, description="Maximum selection quantity")
    is_required: bool = Field(default=False, description="Selection is required")
    sort_order: int = Field(default=0, ge=0, description="Display order")


class ModifierGroupCreate(ModifierGroupBase):
    """Create modifier group with modifiers"""
    product_ids: List[UUID] = Field(..., min_length=1, description="Product IDs to associate")
    modifiers: List[ModifierCreate] = Field(default=[], description="Modifiers in this group")
    tenant_id: UUID = Field(..., description="Tenant ID")


class ModifierGroupUpdate(BaseModel):
    """Update modifier group"""
    product_ids: Optional[List[UUID]] = Field(None, description="Product IDs to associate")
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    min_qty: Optional[int] = Field(None, ge=0)
    max_qty: Optional[int] = Field(None, ge=1)
    is_required: Optional[bool] = None
    sort_order: Optional[int] = Field(None, ge=0)
    modifiers: Optional[List[ModifierCreate]] = Field(None, description="Updated modifiers")


class ModifierGroup(ModifierGroupBase):
    """Complete modifier group with modifiers"""
    id: UUID
    tenant_id: UUID
    created_at: datetime
    updated_at: datetime

    # Related data - now supports multiple products
    products: List[ProductInfo] = Field(default=[], description="Associated products")
    modifiers: List[Modifier] = []

    class Config:
        from_attributes = True

class ModifierGroupResponse(BaseModel):
    """Single modifier group response"""
    success: bool = True
    data: ModifierGroup

class ModifierGroupsListResponse(BaseModel):
    """List of modifier groups response"""
    success: bool = True
    total: int
    data: List[ModifierGroup]

class ModifierGroupStats(BaseModel):
    """Modifier groups statistics"""
    total_groups: int
    total_modifiers: int
    products_with_modifiers: int

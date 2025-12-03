# Combo models for menu management
from pydantic import BaseModel, Field
from typing import Optional, List
from uuid import UUID
from datetime import datetime
from decimal import Decimal

class ComboItemBase(BaseModel):
    """Base combo item fields"""
    item_product_id: UUID = Field(..., description="Product ID included in combo")
    quantity: int = Field(default=1, ge=1, description="Quantity of this product in combo")
    is_optional: bool = Field(default=False, description="Can be removed from combo")
    is_customizable: bool = Field(default=False, description="Can be customized")
    sort_order: int = Field(default=0, ge=0, description="Display order")
    individual_price: Optional[Decimal] = Field(None, description="Normal price of item")
    combo_price: Optional[Decimal] = Field(None, description="Price in combo (discounted)")
    discount_amount: Optional[Decimal] = Field(None, description="Discount applied")

class ComboItemCreate(ComboItemBase):
    """Create combo item"""
    pass

class ComboItemUpdate(BaseModel):
    """Update combo item fields"""
    item_product_id: Optional[UUID] = None
    quantity: Optional[int] = Field(None, ge=1)
    is_optional: Optional[bool] = None
    is_customizable: Optional[bool] = None
    sort_order: Optional[int] = Field(None, ge=0)
    individual_price: Optional[Decimal] = None
    combo_price: Optional[Decimal] = None
    discount_amount: Optional[Decimal] = None

class ComboItem(ComboItemBase):
    """Complete combo item"""
    id: UUID
    combo_product_id: UUID
    created_at: datetime
    updated_at: datetime

    # Related data
    product_name: Optional[str] = None

    class Config:
        from_attributes = True

class ComboBase(BaseModel):
    """Base combo fields (extends product)"""
    name: str = Field(..., min_length=1, max_length=255, description="Combo name")
    description: Optional[str] = Field(None, description="Combo description")
    price: Decimal = Field(..., gt=0, description="Combo total price")
    category_id: Optional[UUID] = Field(None, description="Category ID")
    is_available: bool = Field(default=True, description="Is available for sale")

class ComboCreate(ComboBase):
    """Create combo with items"""
    items: List[ComboItemCreate] = Field(default=[], description="Products included in combo")
    tenant_id: UUID = Field(..., description="Tenant ID")

class ComboUpdate(BaseModel):
    """Update combo"""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    price: Optional[Decimal] = Field(None, gt=0)
    category_id: Optional[UUID] = None
    is_available: Optional[bool] = None
    items: Optional[List[ComboItemCreate]] = Field(None, description="Updated combo items")

class Combo(ComboBase):
    """Complete combo with items"""
    id: UUID
    tenant_id: UUID
    is_combo: bool = True
    created_at: datetime
    updated_at: datetime

    # Related data
    category_name: Optional[str] = None
    items: List[ComboItem] = []
    total_individual_price: Optional[Decimal] = None
    total_savings: Optional[Decimal] = None

    class Config:
        from_attributes = True

class ComboResponse(BaseModel):
    """Single combo response"""
    success: bool = True
    data: Combo

class CombosListResponse(BaseModel):
    """List of combos response"""
    success: bool = True
    total: int
    data: List[Combo]

class ComboStats(BaseModel):
    """Combo statistics"""
    total_combos: int
    active_combos: int
    total_items: int
    avg_savings: Optional[Decimal] = None

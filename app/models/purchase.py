from pydantic import BaseModel, Field
from datetime import datetime, date
from typing import Optional, List
from uuid import UUID
from decimal import Decimal

# Purchase Item Models
class PurchaseItemBase(BaseModel):
    ingredient_id: UUID = Field(..., description="ID of the ingredient")
    quantity: Decimal = Field(..., gt=0, description="Quantity purchased")
    unit: str = Field(..., description="Unit of measure")
    unit_cost: Decimal = Field(..., gt=0, description="Cost per unit")
    total_cost: Optional[Decimal] = Field(None, description="Total cost (calculated)")
    expiry_date: Optional[date] = Field(None, description="Expiry date of the ingredient")
    batch_number: Optional[str] = Field(None, description="Batch or lot number")
    notes: Optional[str] = Field(None, description="Additional notes")

class PurchaseItemCreate(PurchaseItemBase):
    pass

class PurchaseItemUpdate(BaseModel):
    ingredient_id: Optional[UUID] = None
    quantity: Optional[Decimal] = Field(None, gt=0)
    unit: Optional[str] = None
    unit_cost: Optional[Decimal] = Field(None, gt=0)
    total_cost: Optional[Decimal] = None
    expiry_date: Optional[date] = None
    batch_number: Optional[str] = None
    notes: Optional[str] = None

class PurchaseItem(PurchaseItemBase):
    id: UUID
    purchase_id: UUID
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True

# Purchase Models
class PurchaseBase(BaseModel):
    supplier_id: Optional[UUID] = Field(None, description="Supplier ID")
    purchase_number: Optional[str] = Field(None, description="Purchase order number (auto-generated if not provided)")
    purchase_date: Optional[datetime] = Field(None, description="Date of purchase")
    delivery_date: Optional[datetime] = Field(None, description="Expected delivery date")
    total_amount: Optional[Decimal] = Field(None, ge=0, description="Total amount")
    tax_amount: Optional[Decimal] = Field(None, ge=0, description="Tax amount")
    status: Optional[str] = Field("pending", description="Purchase status")
    invoice_number: Optional[str] = Field(None, description="Invoice number")
    notes: Optional[str] = Field(None, description="Additional notes")

class PurchaseCreate(PurchaseBase):
    items: List[PurchaseItemCreate] = Field(default_factory=list, description="Purchase items")

class PurchaseUpdate(BaseModel):
    supplier_id: Optional[UUID] = None
    purchase_number: Optional[str] = None
    purchase_date: Optional[datetime] = None
    delivery_date: Optional[datetime] = None
    total_amount: Optional[Decimal] = None
    tax_amount: Optional[Decimal] = None
    status: Optional[str] = None
    invoice_number: Optional[str] = None
    notes: Optional[str] = None
    items: Optional[List[PurchaseItemCreate]] = None

class Purchase(PurchaseBase):
    id: UUID
    tenant_id: UUID
    created_by: Optional[UUID] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    items: List[PurchaseItem] = Field(default_factory=list)

    class Config:
        from_attributes = True

class PurchaseResponse(BaseModel):
    success: bool = True
    data: Purchase

class PurchasesListResponse(BaseModel):
    success: bool = True
    data: List[Purchase]
    total: int
    page: int = 1
    limit: int = 50

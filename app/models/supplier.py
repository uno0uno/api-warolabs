from pydantic import BaseModel, Field, EmailStr
from datetime import datetime
from typing import Optional, Any, Dict, List
from uuid import UUID

class SupplierBase(BaseModel):
    name: str = Field(..., description="Supplier name")
    contact_info: Optional[Dict[str, Any]] = Field(None, description="Contact information as JSON")
    tax_id: Optional[str] = Field(None, description="Tax ID or NIT")
    address: Optional[str] = Field(None, description="Supplier address")
    phone: Optional[str] = Field(None, description="Phone number")
    email: Optional[EmailStr] = Field(None, description="Email address")
    payment_terms: Optional[str] = Field(None, description="Payment terms")
    is_active: bool = Field(True, description="Whether supplier is active")

class SupplierCreate(SupplierBase):
    payment_agreements: Optional[List[Dict[str, Any]]] = Field(None, description="Payment agreements to create")

class SupplierUpdate(BaseModel):
    name: Optional[str] = None
    contact_info: Optional[Dict[str, Any]] = None
    tax_id: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[EmailStr] = None
    payment_terms: Optional[str] = None
    is_active: Optional[bool] = None

class Supplier(SupplierBase):
    id: UUID
    tenant_id: UUID = Field(alias='tenantId')
    access_token: Optional[UUID] = None
    created_at: datetime = Field(alias='createdAt')
    updated_at: datetime = Field(alias='updatedAt')

    class Config:
        from_attributes = True
        populate_by_name = True

class SupplierResponse(BaseModel):
    success: bool = True
    data: Supplier

class SupplierStats(BaseModel):
    activos: int = Field(description="Number of active suppliers")
    inactivos: int = Field(description="Number of inactive suppliers")
    promedio_pago: int = Field(description="Average payment terms in days")
    con_entregas: int = Field(description="Suppliers with scheduled deliveries")

class SuppliersListResponse(BaseModel):
    success: bool = True
    data: list[Supplier]
    total: int
    page: int = 1
    limit: int = 50
    stats: Optional[SupplierStats] = None
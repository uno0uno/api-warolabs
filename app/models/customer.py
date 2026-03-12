"""
Customer Models - Pydantic schemas for customer management
"""
from pydantic import BaseModel, Field
from typing import Optional, List
from uuid import UUID
from datetime import datetime


class CustomerBase(BaseModel):
    """Base customer fields"""
    phone_number: str = Field(..., min_length=7, max_length=20, description="Customer phone number")
    name: Optional[str] = Field(None, max_length=255, description="Customer name")
    email: Optional[str] = Field(None, description="Customer email")


class CustomerSearchOrCreate(BaseModel):
    """Model for searching or creating a customer"""
    phone_number: str = Field(..., min_length=7, max_length=20)
    name: Optional[str] = None
    email: Optional[str] = None


class Customer(CustomerBase):
    """Complete customer model"""
    id: UUID
    tenant_id: Optional[UUID] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class CustomerResponse(BaseModel):
    """Single customer response"""
    success: bool = True
    data: Customer
    is_new: bool = Field(..., description="True if customer was just created")


class CustomerSearchResponse(BaseModel):
    """Customer search response"""
    success: bool = True
    customer: Optional[Customer] = None
    found: bool = Field(..., description="True if customer was found")


class CustomerSummary(BaseModel):
    """Minimal customer data for search results"""
    id: UUID
    name: Optional[str] = None
    phone_number: Optional[str] = None


class CustomerQuerySearchResponse(BaseModel):
    """Response for partial-match customer search"""
    success: bool = True
    data: List[CustomerSummary]

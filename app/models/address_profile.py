"""
Address Profile Models
Pydantic models for customer delivery addresses (online ordering)
"""
from pydantic import BaseModel, Field
from typing import Optional
from uuid import UUID
from datetime import datetime


class AddressProfileCreate(BaseModel):
    """Create new delivery address"""
    customer_id: UUID
    address_line1: str = Field(..., min_length=5, max_length=200)
    address_line2: Optional[str] = Field(None, max_length=200)
    city: str = Field(..., min_length=2, max_length=100)
    state: str = Field(..., min_length=2, max_length=100)
    postal_code: str = Field(..., min_length=4, max_length=20)
    country: str = Field(default="CO", max_length=2)
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    longitude: Optional[float] = Field(None, ge=-180, le=180)
    is_default: bool = Field(default=False)
    address_type: str = Field(default="home", pattern="^(home|work|other)$")
    delivery_notes: Optional[str] = Field(None, max_length=500)


class AddressProfileUpdate(BaseModel):
    """Update existing delivery address"""
    address_line1: Optional[str] = Field(None, min_length=5, max_length=200)
    address_line2: Optional[str] = Field(None, max_length=200)
    city: Optional[str] = Field(None, min_length=2, max_length=100)
    state: Optional[str] = Field(None, min_length=2, max_length=100)
    postal_code: Optional[str] = Field(None, min_length=4, max_length=20)
    country: Optional[str] = Field(None, max_length=2)
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    longitude: Optional[float] = Field(None, ge=-180, le=180)
    is_default: Optional[bool] = None
    address_type: Optional[str] = Field(None, pattern="^(home|work|other)$")
    delivery_notes: Optional[str] = Field(None, max_length=500)


class AddressProfileResponse(BaseModel):
    """Address response model"""
    id: UUID
    customer_id: UUID
    address_line1: str
    address_line2: Optional[str]
    city: str
    state: str
    postal_code: str
    country: str
    latitude: Optional[float]
    longitude: Optional[float]
    is_default: bool
    address_type: str
    delivery_notes: Optional[str]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AddressProfileList(BaseModel):
    """List of addresses for customer"""
    addresses: list[AddressProfileResponse]
    total: int
    default_address_id: Optional[UUID] = None



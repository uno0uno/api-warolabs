from pydantic import BaseModel, Field
from datetime import datetime, date, time
from typing import Optional, Any, Dict, Literal
from uuid import UUID

class PaymentAgreementBase(BaseModel):
    name: str = Field(..., description="Agreement name")
    description: Optional[str] = Field(None, description="Agreement description")
    agreement_type: Literal['same_day', 'days_after_delivery', 'specific_day_month', 'end_of_month', 'custom'] = Field(
        ...,
        description="Type of payment agreement"
    )
    days_offset: Optional[int] = Field(None, description="Days after delivery (for days_after_delivery type)")
    specific_day: Optional[int] = Field(None, ge=1, le=31, description="Specific day of month (1-31)")
    payment_hour: Optional[time] = Field(default=time(23, 59, 0), description="Hour of day for payment")
    valid_from: Optional[date] = Field(None, description="When this agreement starts")
    valid_until: Optional[date] = Field(None, description="When it ends (NULL = indefinite)")
    auto_apply: bool = Field(False, description="Automatically apply to new purchases")
    is_active: bool = Field(True, description="Whether agreement is active")
    priority: int = Field(0, description="Priority when multiple agreements exist")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Additional metadata")

class PaymentAgreementCreate(PaymentAgreementBase):
    pass

class PaymentAgreementUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    agreement_type: Optional[Literal['same_day', 'days_after_delivery', 'specific_day_month', 'end_of_month', 'custom']] = None
    days_offset: Optional[int] = None
    specific_day: Optional[int] = Field(None, ge=1, le=31)
    payment_hour: Optional[time] = None
    valid_from: Optional[date] = None
    valid_until: Optional[date] = None
    auto_apply: Optional[bool] = None
    is_active: Optional[bool] = None
    priority: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None

class PaymentAgreement(PaymentAgreementBase):
    id: UUID
    tenant_id: UUID = Field(alias='tenantId')
    supplier_id: UUID = Field(alias='supplierId')
    created_at: datetime = Field(alias='createdAt')
    updated_at: datetime = Field(alias='updatedAt')
    created_by: Optional[UUID] = Field(None, alias='createdBy')

    class Config:
        from_attributes = True
        populate_by_name = True

class PaymentAgreementResponse(BaseModel):
    success: bool = True
    data: PaymentAgreement

class PaymentAgreementsListResponse(BaseModel):
    success: bool = True
    data: list[PaymentAgreement]
    total: int

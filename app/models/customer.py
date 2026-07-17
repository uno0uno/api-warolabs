"""
Customer Models - Pydantic schemas for customer management
"""
from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Any, Optional, List, Literal
from uuid import UUID
from datetime import datetime


# DIAN identification document types accepted on customer profile
# (mapped to Matias identity_document_id in api_facturacion)
FiscalIdType = Literal['CC', 'CE', 'NIT', 'PA', 'TI']


def normalize_fiscal_id(value: Any) -> Optional[str]:
    """Keep only Matias/DIAN-safe alphanumeric document characters."""
    if value is None:
        return None
    normalized = ''.join(ch for ch in str(value).strip().upper() if ch.isalnum())
    return normalized or None


class FiscalDataMixin(BaseModel):
    """Optional fiscal fields used when emitting an identified electronic invoice."""
    fiscal_id_type: Optional[FiscalIdType] = Field(None, description="DIAN doc type: CC, CE, NIT, PA, TI")
    fiscal_id: Optional[str] = Field(None, max_length=30, description="Document number (NIT without DV)")
    fiscal_business_name: Optional[str] = Field(None, max_length=255, description="Razón social or legal name")
    fiscal_email: Optional[str] = Field(None, description="Email used for the invoice (overrides general email)")

    @field_validator('fiscal_id', mode='before')
    @classmethod
    def normalize_fiscal_id_field(cls, value):
        return normalize_fiscal_id(value)

    @model_validator(mode='after')
    def validate_fiscal_triplet(self):
        any_provided = any([self.fiscal_id_type, self.fiscal_id, self.fiscal_business_name])
        all_provided = all([self.fiscal_id_type, self.fiscal_id, self.fiscal_business_name])
        if any_provided and not all_provided:
            raise ValueError(
                "fiscal_id_type, fiscal_id and fiscal_business_name must be provided together"
            )
        return self


class CustomerBase(BaseModel):
    """Base customer fields"""
    phone_number: str = Field(..., min_length=7, max_length=20, description="Customer phone number")
    name: Optional[str] = Field(None, max_length=255, description="Customer name")
    email: Optional[str] = Field(None, description="Customer email")


class CustomerSearchOrCreate(FiscalDataMixin):
    """Model for searching or creating a customer"""
    phone_number: str = Field(..., min_length=7, max_length=20)
    name: Optional[str] = None
    email: Optional[str] = None


class Customer(CustomerBase, FiscalDataMixin):
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
    """Minimal customer data for search results.
    Includes fiscal_id so the POS search results can show the cédula/NIT
    that matched, helping the cashier confirm the right customer when
    several share a similar name (Issue #526 follow-up).
    """
    id: UUID
    name: Optional[str] = None
    phone_number: Optional[str] = None
    email: Optional[str] = None
    fiscal_id: Optional[str] = None
    fiscal_id_type: Optional[str] = None
    fiscal_business_name: Optional[str] = None


class CustomerUpdate(FiscalDataMixin):
    """Editable customer fields — all optional, only provided fields are updated"""
    name: Optional[str] = Field(None, max_length=255)
    email: Optional[str] = Field(None)
    phone_number: Optional[str] = Field(None, min_length=7, max_length=20)


class CustomerUpdateResponse(BaseModel):
    success: bool = True
    data: Customer


class CustomerQuerySearchResponse(BaseModel):
    """Response for partial-match customer search"""
    success: bool = True
    data: List[CustomerSummary]


class TopProduct(BaseModel):
    """Single product entry in top products list"""
    name: str
    count: int


class CustomerInsights(BaseModel):
    """Aggregated purchase stats for a customer, scoped to a tenant"""
    orders_count: int
    last_order_date: Optional[datetime] = None
    avg_ticket: Optional[int] = None
    top_products: Optional[List[TopProduct]] = None
    avg_days_between_visits: Optional[float] = None


class CustomerInsightsResponse(BaseModel):
    """Response for customer insights endpoint"""
    success: bool = True
    data: CustomerInsights

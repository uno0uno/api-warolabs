"""Pydantic v2 models for tenant tax configuration."""
from decimal import Decimal
from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, ConfigDict


class TaxConfigResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    inc_applicable: bool
    inc_rate: Decimal
    inc_gl_account_code: str
    inc_included_in_price: bool
    iva_applicable: bool
    iva_rate: Decimal
    iva_gl_account_code: str
    iva_included_in_price: bool
    liquor_tax_applicable: bool
    liquor_tax_rate: Decimal
    liquor_tax_gl_account_code: str
    created_at: datetime
    updated_at: datetime


class TaxConfigUpdate(BaseModel):
    inc_applicable: bool
    inc_included_in_price: bool
    iva_applicable: bool
    iva_included_in_price: bool
    liquor_tax_applicable: bool

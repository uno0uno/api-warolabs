"""Pydantic v2 models for tenant tax configuration."""
from decimal import Decimal
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field


class TaxConfigResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    inc_applicable: bool
    inc_rate: Decimal
    inc_gl_account_code: str
    inc_gl_account_id: Optional[UUID] = None
    inc_included_in_price: bool
    iva_applicable: bool
    iva_rate: Decimal
    iva_gl_account_code: str
    iva_gl_account_id: Optional[UUID] = None
    iva_included_in_price: bool
    liquor_tax_applicable: bool
    liquor_tax_rate: Decimal
    liquor_tax_gl_account_code: str
    liquor_tax_gl_account_id: Optional[UUID] = None
    tax_lines: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Optional profile tax_lines[]; when null, INC/IVA/liquor columns adapt",
    )
    category_map: Optional[Dict[str, Optional[str]]] = Field(
        default=None,
        description="Optional product tax_category → tax line key",
    )
    tax_jurisdiction_code: Optional[str] = Field(
        default=None,
        description="US state or CA province code when country requires jurisdiction",
    )
    commercial_tax_applicable: bool = Field(
        default=False,
        description="When tax_lines present: apply commercial tax in POS/Menú if true",
    )
    created_at: datetime
    updated_at: datetime


class TaxConfigUpdate(BaseModel):
    inc_applicable: bool
    inc_included_in_price: bool
    iva_applicable: bool
    iva_included_in_price: bool
    liquor_tax_applicable: bool
    inc_gl_account_id: Optional[UUID] = None
    iva_gl_account_id: Optional[UUID] = None
    liquor_tax_gl_account_id: Optional[UUID] = None
    tax_lines: Optional[List[Dict[str, Any]]] = None
    category_map: Optional[Dict[str, Optional[str]]] = None
    tax_jurisdiction_code: Optional[str] = None
    commercial_tax_applicable: Optional[bool] = Field(
        default=None,
        description="Commercial on/off; omit on CO-only updates to leave unchanged",
    )
    # CO column bridge (#1873): optional editable rates; omit to leave unchanged.
    iva_rate: Optional[Decimal] = None
    inc_rate: Optional[Decimal] = None
    liquor_tax_rate: Optional[Decimal] = None

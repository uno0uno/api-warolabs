"""
Pydantic response models and request body models for payment method groups and methods.
Issue: https://github.com/uno0uno/warocol.com/issues/330
Issue: https://github.com/uno0uno/warocol.com/issues/331
"""
from pydantic import BaseModel, ConfigDict
from typing import Optional, List


# ── Response models ────────────────────────────────────────────────────────────

class PaymentMethodGroup(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenantId: Optional[str]
    name: str
    slug: str
    triggersCartera: bool
    isActive: bool
    sortOrder: int
    glAccountCode: Optional[str] = None


class PaymentMethodGroupWithCount(PaymentMethodGroup):
    """Group response including the number of active methods belonging to the tenant."""
    methodCount: int = 0


class PaymentMethod(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenantId: str
    groupId: str
    name: str
    isActive: bool
    sortOrder: int


class PaymentMethodGroupWithMethods(PaymentMethodGroup):
    methods: List[PaymentMethod] = []


# ── POS read-only response (lightweight) ──────────────────────────────────────

class PosPaymentMethod(BaseModel):
    id: str
    name: str


class PosPaymentMethodGroup(BaseModel):
    id: str
    name: str
    slug: str
    triggersCartera: bool
    methods: List[PosPaymentMethod] = []


# ── Request body models ────────────────────────────────────────────────────────

class CreateGroupRequest(BaseModel):
    name: str
    slug: str
    triggersCartera: bool = False
    sortOrder: int = 0


class PatchGroupRequest(BaseModel):
    name: Optional[str] = None
    isActive: Optional[bool] = None
    sortOrder: Optional[int] = None
    triggersCartera: Optional[bool] = None
    glAccountCode: Optional[str] = None


class CreateMethodRequest(BaseModel):
    groupId: str
    name: str
    sortOrder: int = 0


class PatchMethodRequest(BaseModel):
    name: Optional[str] = None
    groupId: Optional[str] = None
    isActive: Optional[bool] = None
    sortOrder: Optional[int] = None

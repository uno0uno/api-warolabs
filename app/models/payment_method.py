"""
Pydantic response models for payment method groups and methods.
Issue: https://github.com/uno0uno/warocol.com/issues/330
"""
from pydantic import BaseModel, ConfigDict
from typing import Optional, List


class PaymentMethodGroup(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenantId: Optional[str]
    name: str
    slug: str
    triggersCartera: bool
    isActive: bool
    sortOrder: int


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

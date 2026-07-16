from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class WarehouseCategoryCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)


class WarehouseCategoryUpdate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)


class WarehouseCategory(BaseModel):
    id: UUID
    tenant_id: Optional[UUID] = None
    name: str
    normalized_name: str
    is_active: bool
    scope: str
    can_manage: bool
    ingredient_count: int = 0
    global_count: int = 0
    tenant_count: int = 0
    created_at: datetime
    updated_at: datetime


class WarehouseCategoryResponse(BaseModel):
    success: bool = True
    data: WarehouseCategory


class WarehouseCategoriesResponse(BaseModel):
    success: bool = True
    total: int
    data: List[WarehouseCategory]

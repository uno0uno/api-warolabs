# Category models
from pydantic import BaseModel
from typing import Optional, List
from uuid import UUID
from datetime import datetime

class CategoryBase(BaseModel):
    """Base category fields"""
    name: str
    description: Optional[str] = None

class Category(CategoryBase):
    """Complete category"""
    id: UUID
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

class CategoriesListResponse(BaseModel):
    """List of categories response"""
    success: bool = True
    total: int
    data: List[Category]

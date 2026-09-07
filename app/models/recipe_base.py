"""
Recipe Base Models - Pydantic schemas for recipe base types and templates
"""
from pydantic import BaseModel, Field
from typing import Optional, List
from uuid import UUID
from datetime import datetime


# ==================== Recipe Base Template Ingredient ====================

class RecipeBaseIngredientBase(BaseModel):
    """Base model for recipe base ingredients"""
    ingredient_id: UUID
    base_quantity: float = Field(..., gt=0)
    unit: str = Field(..., min_length=1, max_length=50)
    is_required: bool = True
    notes: Optional[str] = None


class RecipeBaseIngredientCreate(RecipeBaseIngredientBase):
    """Model for creating a recipe base ingredient"""
    pass


class RecipeBaseIngredient(RecipeBaseIngredientBase):
    """Full recipe base ingredient model with all fields"""
    id: UUID
    product_base_type_id: UUID
    tenant_id: UUID
    created_at: datetime
    updated_at: datetime

    # Populated from joins
    ingredient_name: Optional[str] = None
    costo_unitario: Optional[float] = 0
    controla_inventario: Optional[bool] = False
    # Stock unit + weight for ml|gr ↔ und costing (#704); costo_linea is converted.
    stock_unit: Optional[str] = None
    unit_weight_gr: Optional[float] = None
    unit_weight_unit: Optional[str] = None
    costo_linea: Optional[float] = 0

    class Config:
        from_attributes = True


# ==================== Recipe Base Type (Product Base Type) ====================

class RecipeBaseTypeBase(BaseModel):
    """Base model for recipe base types"""
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    is_active: bool = True


class RecipeBaseTypeCreate(RecipeBaseTypeBase):
    """Model for creating a recipe base type with its ingredients"""
    ingredients: List[RecipeBaseIngredientCreate] = []
    tenant_id: Optional[UUID] = None  # Will be populated from session


class RecipeBaseTypeUpdate(BaseModel):
    """Model for updating a recipe base type"""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    is_active: Optional[bool] = None
    ingredients: Optional[List[RecipeBaseIngredientCreate]] = None


class RecipeBaseType(RecipeBaseTypeBase):
    """Full recipe base type model"""
    id: UUID
    created_at: datetime
    updated_at: datetime

    # Optional: include ingredients when requested
    ingredients: List[RecipeBaseIngredient] = []

    class Config:
        from_attributes = True


# ==================== Response Models ====================

class RecipeBaseTypeResponse(BaseModel):
    """Response model for single recipe base type"""
    success: bool = True
    data: RecipeBaseType


class RecipeBaseTypesListResponse(BaseModel):
    """Response model for list of recipe base types"""
    success: bool = True
    total: int
    data: List[RecipeBaseType]

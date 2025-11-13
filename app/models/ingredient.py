# This data model for Ingredients is based on the 'ingredients' table schema
# retrieved from the database, and augmented with assumed fields for price and supplier_id
# which may come from related tables or services.
from pydantic import BaseModel, Field
from typing import Optional, List
from uuid import UUID
from datetime import datetime

class IngredientBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, description="Name of the ingredient")
    unit: str = Field(..., min_length=1, max_length=50, description="Unit of measure (e.g., kg, liter, unit)")
    category: Optional[str] = Field(None, max_length=255, description="Category of the ingredient")
    description: Optional[str] = Field(None, max_length=1024, description="Detailed description of the ingredient")
    minimum_order_quantity: Optional[float] = Field(None, gt=0, description="Minimum order quantity for the ingredient")
    
    # Assuming price and supplier_id might come from a join or another service
    price: Optional[float] = Field(None, gt=0, description="Current price of the ingredient")
    supplier_id: Optional[UUID] = Field(None, description="ID of the primary supplier for this ingredient")


class IngredientCreate(IngredientBase):
    pass

class IngredientUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    unit: Optional[str] = Field(None, min_length=1, max_length=50)
    category: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = Field(None, max_length=1024)
    minimum_order_quantity: Optional[float] = Field(None, gt=0)
    price: Optional[float] = Field(None, gt=0)
    supplier_id: Optional[UUID] = None

class Ingredient(IngredientBase):
    id: UUID
    tenant_id: Optional[UUID] # tenant_id is nullable in DB
    created_at: Optional[datetime]
    updated_at: Optional[datetime]

    class Config:
        from_attributes = True

class IngredientResponse(BaseModel):
    success: bool = True
    data: Ingredient

class IngredientsListResponse(BaseModel):
    success: bool = True
    total: int
    data: List[Ingredient]

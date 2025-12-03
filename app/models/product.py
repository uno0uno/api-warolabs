# Product models for menu management
from pydantic import BaseModel, Field
from typing import Optional, List
from uuid import UUID
from datetime import datetime
from decimal import Decimal

class RecipeIngredientBase(BaseModel):
    """Ingredient in a product recipe"""
    ingredient_id: UUID = Field(..., description="ID of the ingredient")
    quantity: float = Field(..., gt=0, description="Quantity of ingredient needed")
    unit: str = Field(..., min_length=1, max_length=50, description="Unit of measure (g, ml, kg, l, u)")

class RecipeIngredient(RecipeIngredientBase):
    """Recipe ingredient with full details"""
    id: UUID
    product_id: UUID
    ingredient_name: Optional[str] = None
    ingredient_cost_per_unit: Optional[Decimal] = None
    total_cost: Optional[Decimal] = None  # quantity * cost_per_unit

    class Config:
        from_attributes = True

class ProductBase(BaseModel):
    """Base product fields"""
    name: str = Field(..., min_length=1, max_length=255, description="Product name")
    description: Optional[str] = Field(None, description="Product description")
    price: Decimal = Field(..., gt=0, description="Sale price")
    category_id: UUID = Field(..., description="Category ID")
    product_base_type_id: Optional[UUID] = Field(None, description="Optional recipe base type ID (deprecated, use recipe_base_ids)")
    preparation_time: Optional[int] = Field(None, ge=0, description="Preparation time in minutes")
    controla_stock: bool = Field(True, description="Whether to control stock")
    is_available: bool = Field(True, description="Whether product is available")
    is_combo: bool = Field(False, description="Whether product is a combo")
    allow_modifiers: bool = Field(True, description="Whether product allows modifiers")

class ProductCreate(ProductBase):
    """Create product with recipe"""
    ingredients: List[RecipeIngredientBase] = Field(default=[], description="Recipe ingredients")
    recipe_base_ids: List[UUID] = Field(default=[], description="List of recipe base IDs")
    tenant_id: UUID = Field(..., description="Tenant ID")

class ProductUpdate(BaseModel):
    """Update product fields"""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    price: Optional[Decimal] = Field(None, gt=0)
    category_id: Optional[UUID] = None
    product_base_type_id: Optional[UUID] = Field(None, description="Optional recipe base type ID (deprecated)")
    recipe_base_ids: Optional[List[UUID]] = Field(None, description="List of recipe base IDs")
    preparation_time: Optional[int] = Field(None, ge=0)
    controla_stock: Optional[bool] = None
    is_available: Optional[bool] = None
    is_combo: Optional[bool] = None
    allow_modifiers: Optional[bool] = None
    ingredients: Optional[List[RecipeIngredientBase]] = Field(None, description="Updated recipe ingredients")

class Product(ProductBase):
    """Complete product with calculated fields"""
    id: UUID
    tenant_id: Optional[UUID]
    costo_calculado: Optional[Decimal] = Field(None, description="Calculated cost from recipe")
    precio_sugerido: Optional[Decimal] = Field(None, description="Suggested price")
    margen_objetivo: Optional[Decimal] = Field(None, description="Target margin")
    created_at: datetime
    updated_at: datetime

    # Related data
    category_name: Optional[str] = None
    ingredients: List[RecipeIngredient] = []
    recipe_base_ids: List[UUID] = Field(default=[], description="List of associated recipe base IDs")

    # Calculated fields
    margen_porcentaje: Optional[float] = None  # (price - cost) / cost * 100
    margen_valor: Optional[Decimal] = None  # price - cost

    class Config:
        from_attributes = True

class ProductResponse(BaseModel):
    """Single product response"""
    success: bool = True
    data: Product

class ProductsListResponse(BaseModel):
    """List of products response"""
    success: bool = True
    total: int
    data: List[Product]

class ProductStats(BaseModel):
    """Product statistics for dashboard"""
    total: int
    available: int
    with_stock_control: int
    combos: int
    avg_margin: Optional[float] = None

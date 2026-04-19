# Product models for menu management
from pydantic import BaseModel, Field, model_validator
from typing import Optional, List, Literal
from uuid import UUID
from datetime import datetime
from decimal import Decimal

class Modifier(BaseModel):
    """Modifier option within a modifier group"""
    id: UUID
    name: str
    price: Decimal
    is_available: Optional[bool] = True
    is_default: Optional[bool] = False
    sort_order: Optional[int] = None

    class Config:
        from_attributes = True

class ModifierGroup(BaseModel):
    """Group of modifiers for a product"""
    id: UUID
    name: str
    min_qty: int
    max_qty: int
    is_required: bool
    sort_order: Optional[int] = None
    modifiers: List[Modifier] = []

    class Config:
        from_attributes = True

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
    # DEPRECATED: controla_stock is now ALWAYS True. All products control inventory automatically.
    # This field is kept for database compatibility but is ignored in logic.
    controla_stock: bool = Field(True, description="DEPRECATED: Always True. All products control inventory")
    is_available: bool = Field(True, description="Whether product is available")
    is_available_online: bool = Field(True, description="Whether product is available for online ordering (delivery/pickup)")
    is_combo: bool = Field(False, description="Whether product is a combo")
    is_resale: bool = Field(False, description="Whether product is a resale product (not prepared)")
    allow_modifiers: bool = Field(True, description="Whether product allows modifiers")
    tax_category: Literal['standard', 'liquor', 'exempt'] = Field("standard", description="Tax classification: standard (INC/IVA), liquor (IVA licores 5%), exempt (no tax)")

class ProductCreate(ProductBase):
    """Create product with recipe"""
    # Products must have at least one ingredient OR at least one recipe base
    # Both can be present, but at least one is required for inventory control
    ingredients: List[RecipeIngredientBase] = Field(
        default=[],
        description="Recipe ingredients (optional if recipe_base_ids provided)"
    )
    recipe_base_ids: List[UUID] = Field(default=[], description="List of recipe base IDs")
    tenant_id: UUID = Field(..., description="Tenant ID")

    @model_validator(mode='after')
    def validate_has_ingredients_or_recipe_bases(self):
        """Ensure product has at least one ingredient or one recipe base"""
        if not self.ingredients and not self.recipe_base_ids:
            raise ValueError("El producto debe tener al menos un ingrediente o una receta base")
        return self

class ProductUpdate(BaseModel):
    """Update product fields"""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    price: Optional[Decimal] = Field(None, gt=0)
    category_id: Optional[UUID] = None
    product_base_type_id: Optional[UUID] = Field(None, description="Optional recipe base type ID (deprecated)")
    recipe_base_ids: Optional[List[UUID]] = Field(None, description="List of recipe base IDs")
    preparation_time: Optional[int] = Field(None, ge=0)
    # DEPRECATED: controla_stock is ignored - all products always control inventory
    controla_stock: Optional[bool] = Field(None, description="DEPRECATED: Ignored, always True")
    is_available: Optional[bool] = None
    is_available_online: Optional[bool] = None
    is_combo: Optional[bool] = None
    is_resale: Optional[bool] = None
    allow_modifiers: Optional[bool] = None
    tax_category: Optional[Literal['standard', 'liquor', 'exempt']] = Field(None, description="Tax classification: standard, liquor, exempt")
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
    modifier_groups: List[ModifierGroup] = Field(default=[], description="Modifier groups for this product")

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

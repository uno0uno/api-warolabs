# Product models for menu management
from pydantic import BaseModel, Field
from typing import Optional, List, Literal, Dict, Any
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

class RecipeBaseLink(BaseModel):
    """Per-product link to a recipe base with multiplier (Issue #517)."""
    recipe_base_id: UUID = Field(..., description="ID of the recipe base (product_base_type)")
    quantity: Decimal = Field(
        default=Decimal("1"),
        gt=Decimal("0"),
        description="How many units of the recipe this product consumes (default 1)"
    )

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
    is_available_table_qr: bool = Field(False, description="Whether product appears on the table QR menu")
    is_combo: bool = Field(False, description="Whether product is a combo")
    is_resale: bool = Field(False, description="Whether product is a resale product (not prepared)")
    open_priced: bool = Field(
        False,
        description="When true, POS may send a custom unit_price (venta libre); at most one per tenant",
    )
    allow_modifiers: bool = Field(True, description="Whether product allows modifiers")
    tax_category: Literal['standard', 'liquor', 'exempt'] = Field("standard", description="Tax classification: standard (INC/IVA), liquor (IVA licores 5%), exempt (no tax)")
    station_id: Optional[UUID] = None
    kitchen_name: Optional[str] = Field(None, max_length=100)
    image_url: Optional[str] = Field(None, max_length=500, description="Public URL of the product hero image (Cloudflare R2)")

class ProductCreate(ProductBase):
    """Create product with recipe.

    Products MAY have zero ingredients and zero recipe bases — those products
    do not control inventory (no rows are written to product_recipes /
    product_base_recipes, and stock deduction is a no-op at order completion).
    Used for service-like items (delivery surcharge, tip), pre-packed goods
    that don't warrant inventory tracking, and resale products without recipe.
    """
    ingredients: List[RecipeIngredientBase] = Field(
        default=[],
        description="Recipe ingredients (empty list = no inventory tracking)"
    )
    recipe_base_ids: List[UUID] = Field(
        default=[],
        description="DEPRECATED: List of recipe base IDs (each treated as quantity=1). Prefer recipe_bases for new clients."
    )
    recipe_bases: Optional[List[RecipeBaseLink]] = Field(
        default=None,
        description="Recipe bases with per-product quantity multiplier. Preferred over recipe_base_ids."
    )
    tenant_id: UUID = Field(..., description="Tenant ID")
    costo_percibido: Optional[Decimal] = Field(
        None,
        ge=0,
        description="Operational/perceived unit cost set by the tenant",
    )

class ProductUpdate(BaseModel):
    """Update product fields"""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    price: Optional[Decimal] = Field(None, gt=0)
    category_id: Optional[UUID] = None
    product_base_type_id: Optional[UUID] = Field(None, description="Optional recipe base type ID (deprecated)")
    recipe_base_ids: Optional[List[UUID]] = Field(None, description="DEPRECATED: List of recipe base IDs (each treated as quantity=1). Prefer recipe_bases.")
    recipe_bases: Optional[List[RecipeBaseLink]] = Field(None, description="Recipe bases with per-product quantity multiplier. Preferred over recipe_base_ids.")
    preparation_time: Optional[int] = Field(None, ge=0)
    # DEPRECATED: controla_stock is ignored - all products always control inventory
    controla_stock: Optional[bool] = Field(None, description="DEPRECATED: Ignored, always True")
    is_available: Optional[bool] = None
    is_available_online: Optional[bool] = None
    is_available_table_qr: Optional[bool] = None
    is_combo: Optional[bool] = None
    is_resale: Optional[bool] = None
    open_priced: Optional[bool] = None
    allow_modifiers: Optional[bool] = None
    tax_category: Optional[Literal['standard', 'liquor', 'exempt']] = Field(None, description="Tax classification: standard, liquor, exempt")
    ingredients: Optional[List[RecipeIngredientBase]] = Field(None, description="Updated recipe ingredients")
    station_id: Optional[UUID] = None
    kitchen_name: Optional[str] = Field(None, max_length=100)
    image_url: Optional[str] = Field(None, max_length=500, description="Public URL of the product hero image (null clears it)")
    costo_percibido: Optional[Decimal] = Field(
        None,
        ge=0,
        description="Operational/perceived unit cost (null clears)",
    )

class Product(ProductBase):
    """Complete product with calculated fields"""
    id: UUID
    tenant_id: Optional[UUID]
    costo_calculado: Optional[Decimal] = Field(None, description="Calculated cost from recipe")
    costo_percibido: Optional[Decimal] = Field(
        None,
        description="Operational/perceived cost set by tenant",
    )
    precio_sugerido: Optional[Decimal] = Field(None, description="Suggested price")
    margen_objetivo: Optional[Decimal] = Field(None, description="Target margin")
    created_at: datetime
    updated_at: datetime

    # Station routing
    station: Optional[Dict[str, Any]] = None

    # Related data
    category_name: Optional[str] = None
    ingredients: List[RecipeIngredient] = []
    recipe_base_ids: List[UUID] = Field(default=[], description="DEPRECATED: associated recipe base IDs (sourced from recipe_bases for backwards compat).")
    recipe_bases: List[RecipeBaseLink] = Field(default=[], description="Recipe bases with per-product quantity multiplier (Issue #517).")
    modifier_groups: List[ModifierGroup] = Field(default=[], description="Modifier groups for this product")

    # Calculated fields (real = costo_calculado, operativo = costo_percibido)
    margen_real_pct: Optional[float] = None
    margen_real_valor: Optional[Decimal] = None
    margen_operativo_pct: Optional[float] = None
    margen_operativo_valor: Optional[Decimal] = None
    margen_porcentaje: Optional[float] = None  # alias margen_real_pct (backward compat)
    margen_valor: Optional[Decimal] = None  # alias margen_real_valor

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

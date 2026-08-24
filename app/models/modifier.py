# Modifier models for menu management
from pydantic import BaseModel, Field
from typing import Optional, List, Literal
from uuid import UUID
from datetime import datetime
from decimal import Decimal


ModifierOptionType = Literal["INGREDIENT", "RECIPE", "PRODUCT", "NONE"]


class ProductInfo(BaseModel):
    """Basic product info for modifier group association"""
    id: UUID
    name: str


class IngredientInfo(BaseModel):
    """Ingredient info for modifiers linked to inventory"""
    id: UUID
    name: str
    unit: str
    costo_unitario: Optional[Decimal] = None
    controla_inventario: bool = False
    is_resale: bool = False


class RecipeBaseInfo(BaseModel):
    """Recipe base type linked to a RECIPE modifier option"""
    id: UUID
    name: str


class ModifierRecipeLineBase(BaseModel):
    ingredient_id: UUID
    quantity: Decimal = Field(..., gt=0)
    unit: str = Field(..., min_length=1, max_length=50)


class ModifierRecipeLine(ModifierRecipeLineBase):
    id: UUID
    ingredient: Optional[IngredientInfo] = None


class ModifierBase(BaseModel):
    """Base modifier fields"""
    name: str = Field(..., min_length=1, max_length=255, description="Modifier name")
    price: Decimal = Field(default=0, description="Additional price (can be negative)")
    max_limit: int = Field(default=1, ge=1, description="Maximum quantity allowed")
    included_quantity: int = Field(
        default=0,
        ge=0,
        description="Units included before the additional price applies",
    )
    is_default: bool = Field(default=False, description="Selected by default")
    is_available: bool = Field(default=True, description="Available for selection")
    sort_order: int = Field(default=0, ge=0, description="Display order")
    option_type: ModifierOptionType = Field(
        default="INGREDIENT",
        description="INGREDIENT | RECIPE | PRODUCT | NONE",
    )
    # Ingredient option
    ingredient_id: Optional[UUID] = Field(None, description="Linked ingredient ID")
    ingredient_quantity: Optional[Decimal] = Field(None, description="Quantity of ingredient per modifier")
    ingredient_unit: Optional[str] = Field(None, max_length=20, description="Unit for ingredient quantity")
    # Recipe option (recipe base and/or modifier_recipes BOM)
    recipe_base_type_id: Optional[UUID] = Field(None, description="Recipe base type for RECIPE option")
    recipe_base_quantity: Decimal = Field(default=1, gt=0, description="Multiplier on recipe base template")
    recipe_lines: Optional[List[ModifierRecipeLineBase]] = Field(
        None,
        description="Per-modifier ingredient BOM (modifier_recipes table)",
    )
    # Product option
    linked_product_id: Optional[UUID] = Field(None, description="Menu product for PRODUCT option")
    linked_product_quantity: Decimal = Field(
        default=1,
        gt=0,
        description="Multiplier on linked product composition",
    )


class ModifierCreate(ModifierBase):
    """Create modifier"""
    pass


class ModifierUpdate(BaseModel):
    """Update modifier fields"""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    price: Optional[Decimal] = None
    max_limit: Optional[int] = Field(None, ge=1)
    included_quantity: Optional[int] = Field(None, ge=0)
    is_default: Optional[bool] = None
    is_available: Optional[bool] = None
    sort_order: Optional[int] = Field(None, ge=0)
    option_type: Optional[ModifierOptionType] = None
    ingredient_id: Optional[UUID] = None
    ingredient_quantity: Optional[Decimal] = None
    ingredient_unit: Optional[str] = Field(None, max_length=20)
    recipe_base_type_id: Optional[UUID] = None
    recipe_base_quantity: Optional[Decimal] = Field(None, gt=0)
    recipe_lines: Optional[List[ModifierRecipeLineBase]] = None
    linked_product_id: Optional[UUID] = None
    linked_product_quantity: Optional[Decimal] = Field(None, gt=0)


class Modifier(ModifierBase):
    """Complete modifier"""
    id: UUID
    modifier_group_id: UUID
    created_at: datetime
    updated_at: datetime
    ingredient: Optional[IngredientInfo] = None
    recipe_base: Optional[RecipeBaseInfo] = None
    linked_product: Optional[ProductInfo] = None
    recipe_lines: Optional[List[ModifierRecipeLine]] = None
    unit_cost: Optional[Decimal] = Field(
        None,
        description="Calculated unit cost for one modifier selection (menu preview)",
    )

    class Config:
        from_attributes = True


class ModifierGroupBase(BaseModel):
    """Base modifier group fields"""
    name: str = Field(..., min_length=1, max_length=255, description="Group name")
    min_qty: int = Field(default=0, ge=0, description="Minimum selection quantity")
    max_qty: int = Field(default=1, ge=1, description="Maximum selection quantity")
    is_required: bool = Field(default=False, description="Selection is required")
    sort_order: int = Field(default=0, ge=0, description="Display order")


class ModifierGroupCreate(ModifierGroupBase):
    """Create modifier group with modifiers"""
    # Empty allowed for CSV import / unassigned groups (matrix attach is separate UX).
    product_ids: List[UUID] = Field(
        default_factory=list,
        description="Product IDs to associate (optional; can be empty)",
    )
    modifiers: List[ModifierCreate] = Field(default=[], description="Modifiers in this group")
    tenant_id: Optional[UUID] = Field(None, description="Tenant ID (session preferred)")


class ModifierGroupUpdate(BaseModel):
    """Update modifier group"""
    product_ids: Optional[List[UUID]] = Field(None, description="Product IDs to associate")
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    min_qty: Optional[int] = Field(None, ge=0)
    max_qty: Optional[int] = Field(None, ge=1)
    is_required: Optional[bool] = None
    sort_order: Optional[int] = Field(None, ge=0)
    modifiers: Optional[List[ModifierCreate]] = Field(None, description="Updated modifiers")


class ModifierGroup(ModifierGroupBase):
    """Complete modifier group with modifiers"""
    id: UUID
    tenant_id: UUID
    is_active: bool = Field(default=True, description="Activo/Archivado estado like warehouse_categories.is_active")
    created_at: datetime
    updated_at: datetime

    products: List[ProductInfo] = Field(default=[], description="Associated products")
    modifiers: List[Modifier] = []

    class Config:
        from_attributes = True


class ModifierGroupResponse(BaseModel):
    """Single modifier group response"""
    success: bool = True
    data: ModifierGroup


class ModifierGroupsListResponse(BaseModel):
    """List of modifier groups response"""
    success: bool = True
    total: int
    data: List[ModifierGroup]


class ModifierGroupStats(BaseModel):
    """Modifier groups statistics"""
    total_groups: int
    total_modifiers: int
    products_with_modifiers: int

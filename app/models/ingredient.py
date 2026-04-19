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
    type: Optional[str] = Field('food', max_length=20, description="Type of ingredient: 'food' (alimentos), 'service' (servicios), 'supply' (insumos)")
    description: Optional[str] = Field(None, max_length=1024, description="Detailed description of the ingredient")
    minimum_order_quantity: Optional[float] = Field(None, gt=0, description="Minimum order quantity for the ingredient")
    unit_weight_gr: Optional[float] = Field(None, ge=0, description="Weight in grams of 1 base unit (only for 'und' ingredients). Used for cost calculation in recipes.")

    # Assuming price and supplier_id might come from a join or another service
    price: Optional[float] = Field(None, gt=0, description="Current price of the ingredient")
    supplier_id: Optional[UUID] = Field(None, description="ID of the primary supplier for this ingredient")


class IngredientCreate(IngredientBase):
    pass

class PurchaseUnitInput(BaseModel):
    """
    A purchase unit to create alongside a tenant ingredient.
    Only the key and is_default are accepted from the client — label and
    conversion_factor are resolved server-side from PURCHASE_UNIT_CATALOG.
    """
    purchase_unit: str = Field(..., min_length=1, max_length=50, description="Key from PURCHASE_UNIT_CATALOG")
    is_default: bool = Field(default=False)


class TenantIngredientCreate(BaseModel):
    """Request body for tenant-scoped custom ingredient creation (POST /suppliers/ingredients)."""
    name: str = Field(..., min_length=1, max_length=255)
    unit: str = Field(..., description="Must be one of: gr, ml, kg, und, lt")
    type: Optional[str] = Field(default="food", description="food | service | supply")
    category: Optional[str] = Field(default=None, max_length=255)
    costo_unitario: Optional[float] = Field(default=None, ge=0)
    parent_id: Optional[str] = Field(default=None, description="UUID of a global base ingredient")
    is_resale: Optional[bool] = Field(default=False, description="Mark as resale product — will appear in /menu/reventa")
    unit_weight_gr: Optional[float] = Field(default=None, ge=0, description="Weight in grams of 1 base unit (only for 'und' ingredients)")
    purchase_units: List[PurchaseUnitInput] = Field(default_factory=list, description="Purchase units to create with the ingredient")


class TenantIngredientUpdate(BaseModel):
    """Request body for updating a tenant-scoped custom ingredient (PATCH /suppliers/ingredients/:id)."""
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    unit: Optional[str] = Field(default=None, description="Must be one of: gr, ml, kg, und, lt")
    category: Optional[str] = Field(default=None, max_length=255)
    costo_unitario: Optional[float] = Field(default=None, ge=0)
    parent_id: Optional[str] = Field(default=None, description="UUID of a global base ingredient, or empty string to clear")
    is_resale: Optional[bool] = Field(default=None, description="Mark as resale product")
    purchase_units: Optional[List[PurchaseUnitInput]] = Field(default=None, description="Purchase units to create if the ingredient has none yet")


class IngredientUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    unit: Optional[str] = Field(None, min_length=1, max_length=50)
    category: Optional[str] = Field(None, max_length=255)
    type: Optional[str] = Field(None, max_length=20, description="Type: 'food', 'service', 'supply'")
    description: Optional[str] = Field(None, max_length=1024)
    minimum_order_quantity: Optional[float] = Field(None, gt=0)
    unit_weight_gr: Optional[float] = Field(None, ge=0, description="Weight in grams of 1 base unit (only for 'und' ingredients)")
    price: Optional[float] = Field(None, gt=0)
    supplier_id: Optional[UUID] = None

class Ingredient(IngredientBase):
    id: UUID
    tenant_id: Optional[UUID] # tenant_id is nullable in DB
    created_at: Optional[datetime]
    updated_at: Optional[datetime]
    hierarchy_base_id: Optional[str] = None
    hierarchy_base_name: Optional[str] = None
    has_variants: Optional[int] = None  # count of variants via ingredient_global_hierarchy
    is_custom: Optional[bool] = None   # True when ingredient is tenant-scoped (not global)
    parent_name: Optional[str] = None  # name of the global base ingredient (when parent_id is set)
    default_purchase_unit_label: Optional[str] = None   # label of the default purchase unit
    default_purchase_unit_factor: Optional[float] = None  # conversion factor of the default purchase unit

    class Config:
        from_attributes = True

class IngredientResponse(BaseModel):
    success: bool = True
    data: Ingredient

class IngredientsListResponse(BaseModel):
    success: bool = True
    total: int
    data: List[Ingredient]


# =============================================================================
# INGREDIENT PURCHASE UNITS MODELS
# =============================================================================

class IngredientPurchaseUnitBase(BaseModel):
    """Base fields for ingredient purchase unit configuration"""
    ingredient_id: UUID = Field(..., description="ID of the ingredient")
    purchase_unit: str = Field(..., min_length=1, max_length=50, description="Purchase unit type (paquete, caja, docena, bulto)")
    purchase_unit_label: str = Field(..., min_length=1, max_length=100, description="Display label (e.g., 'Paquete x18', 'Caja x144')")
    conversion_factor: float = Field(..., gt=0, description="Number of base units in 1 purchase unit")
    unit_cost: Optional[float] = Field(None, ge=0, description="Cost per purchase unit")
    is_default: bool = Field(default=False, description="Default purchase unit for this ingredient")
    is_active: bool = Field(default=True, description="Whether this purchase unit is active")
    notes: Optional[str] = Field(None, description="Additional notes")


class IngredientPurchaseUnitCreate(IngredientPurchaseUnitBase):
    """Create ingredient purchase unit"""
    pass


class IngredientPurchaseUnitUpdate(BaseModel):
    """Update ingredient purchase unit"""
    purchase_unit: Optional[str] = Field(None, min_length=1, max_length=50)
    purchase_unit_label: Optional[str] = Field(None, min_length=1, max_length=100)
    conversion_factor: Optional[float] = Field(None, gt=0)
    unit_cost: Optional[float] = Field(None, ge=0)
    is_default: Optional[bool] = None
    is_active: Optional[bool] = None
    notes: Optional[str] = None


class IngredientPurchaseUnit(IngredientPurchaseUnitBase):
    """Complete ingredient purchase unit with metadata"""
    id: UUID
    created_at: datetime
    updated_at: datetime

    # Related data (optional, populated by joins)
    ingredient_name: Optional[str] = None
    ingredient_base_unit: Optional[str] = None

    class Config:
        from_attributes = True


class IngredientPurchaseUnitResponse(BaseModel):
    """Single purchase unit response"""
    success: bool = True
    data: IngredientPurchaseUnit


class IngredientPurchaseUnitsListResponse(BaseModel):
    """List of purchase units response"""
    success: bool = True
    total: int
    data: List[IngredientPurchaseUnit]

from fastapi import APIRouter, Depends, Request, Response, Query, Path
from typing import Optional
from uuid import UUID
from app.core.permissions import Module, require_module
from app.services.ingredient_purchase_units_service import (
    get_all_purchase_units,
    get_purchase_units_by_ingredient,
    get_purchase_unit_by_id,
    create_purchase_unit,
    update_purchase_unit,
    delete_purchase_unit
)
from app.models.ingredient import (
    IngredientPurchaseUnitCreate,
    IngredientPurchaseUnitUpdate,
    IngredientPurchaseUnitResponse,
    IngredientPurchaseUnitsListResponse
)

router = APIRouter()


@router.get("", response_model=IngredientPurchaseUnitsListResponse, dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def list_purchase_units_endpoint(
    request: Request,
    response: Response,
    page: int = Query(default=1, ge=1, description="Page number"),
    limit: int = Query(default=100, ge=1, le=10000, description="Items per page"),
    search: Optional[str] = Query(default=None, description="Search by ingredient name or purchase unit label"),
    ingredient_id: Optional[UUID] = Query(default=None, description="Filter by ingredient ID"),
    active_only: bool = Query(default=True, description="Show only active purchase units")
):
    """
    Get all purchase units with optional filtering
    Requires valid session with tenant context
    """
    return await get_all_purchase_units(
        request, response, page, limit, search, ingredient_id, active_only
    )


@router.get("/ingredient/{ingredient_id}", response_model=IngredientPurchaseUnitsListResponse, dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def get_ingredient_purchase_units_endpoint(
    request: Request,
    response: Response,
    ingredient_id: UUID = Path(..., description="Ingredient ID"),
    active_only: bool = Query(default=True, description="Show only active purchase units")
):
    """
    Get all purchase unit configurations for a specific ingredient
    Requires valid session with tenant context
    """
    return await get_purchase_units_by_ingredient(
        request, response, ingredient_id, active_only
    )


@router.get("/{purchase_unit_id}", response_model=IngredientPurchaseUnitResponse, dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def get_purchase_unit_endpoint(
    request: Request,
    response: Response,
    purchase_unit_id: UUID = Path(..., description="Purchase unit ID")
):
    """
    Get a specific purchase unit configuration by ID
    Requires valid session with tenant context
    """
    return await get_purchase_unit_by_id(request, response, purchase_unit_id)


@router.post("", response_model=IngredientPurchaseUnitResponse, status_code=201, dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def create_purchase_unit_endpoint(
    request: Request,
    response: Response,
    purchase_unit_data: IngredientPurchaseUnitCreate
):
    """
    Create a new purchase unit configuration for an ingredient

    Example:
    {
        "ingredient_id": "uuid-here",
        "purchase_unit": "paquete",
        "purchase_unit_label": "Paquete x18",
        "conversion_factor": 18.0,
        "unit_cost": 15000.00,
        "is_default": true,
        "is_active": true,
        "notes": "Presentación estándar del proveedor"
    }

    Requires valid session with tenant context
    """
    return await create_purchase_unit(request, response, purchase_unit_data)


@router.put("/{purchase_unit_id}", response_model=IngredientPurchaseUnitResponse, dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def update_purchase_unit_endpoint(
    request: Request,
    response: Response,
    purchase_unit_id: UUID = Path(..., description="Purchase unit ID"),
    purchase_unit_data: IngredientPurchaseUnitUpdate = None
):
    """
    Update an existing purchase unit configuration
    Only provided fields will be updated

    Requires valid session with tenant context
    """
    return await update_purchase_unit(request, response, purchase_unit_id, purchase_unit_data)


@router.delete("/{purchase_unit_id}", dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def delete_purchase_unit_endpoint(
    request: Request,
    response: Response,
    purchase_unit_id: UUID = Path(..., description="Purchase unit ID")
):
    """
    Delete a purchase unit configuration
    Requires valid session with tenant context
    """
    return await delete_purchase_unit(request, response, purchase_unit_id)

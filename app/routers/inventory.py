"""
Inventory Router
Endpoints for inventory management and stock tracking
"""
from fastapi import APIRouter, Depends, Request, Response, Query
from typing import Literal, Optional
from uuid import UUID
from app.core.permissions import Module, require_module
from app.services import inventory_service

router = APIRouter(prefix="/inventory", tags=["Inventory"])


@router.get("/stock", dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def get_inventory_stock(
    request: Request,
    response: Response,
    limit: int = Query(250, ge=1, le=500, description="Items per page"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    search: Optional[str] = Query(None, description="Search by ingredient name"),
    status_filter: Optional[str] = Query('all', description="Filter by status: low, critical, zero, ok, all"),
    category: Optional[str] = Query(None, description="Filter by ingredient category"),
    unit: Optional[str] = Query(None, description="Filter by ingredient unit"),
    sort_field: str = Query("current_stock", description="Field to sort by"),
    sort_direction: str = Query("desc", description="Sort direction: asc, desc")
):
    """
    Get current inventory stock with statistics

    Returns inventory with:
    - Current stock levels
    - Stock status (critical, low, ok)
    - Unit costs and total values
    - Statistics summary
    """
    return await inventory_service.get_inventory_stock(
        request,
        response,
        limit=limit,
        offset=offset,
        search=search,
        status_filter=status_filter,
        category=category,
        unit=unit,
        sort_field=sort_field,
        sort_direction=sort_direction
    )


@router.get("/movements", dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def get_inventory_movements(
    request: Request,
    response: Response,
    limit: int = Query(100, ge=1, le=500, description="Items per page"),
    offset: int = Query(0, ge=0, description="Number of items to skip"),
    ingredient_id: Optional[UUID] = Query(None, description="Filter by ingredient ID"),
    movement_type: Optional[str] = Query(None, description="Filter by movement type: purchase, consumption, adjustment, loss, transfer, return"),
    quantity_direction: Optional[Literal["positive", "negative"]] = Query(None, description="Filter by quantity direction: positive, negative"),
    start_date: Optional[str] = Query(None, description="Filter by start date (ISO format)"),
    end_date: Optional[str] = Query(None, description="Filter by end date (ISO format)"),
    search: Optional[str] = Query(None, description="Search by ingredient name or reference number"),
    sort_field: str = Query("created_at", description="Field to sort by"),
    sort_direction: str = Query("desc", description="Sort direction: asc or desc"),
):
    """
    Get inventory movements history

    Returns:
    - All inventory movements (purchases, consumption, adjustments)
    - Previous and new stock levels
    - Reference to source transaction
    - User who made the movement
    """
    return await inventory_service.get_inventory_movements(
        request,
        response,
        limit=limit,
        offset=offset,
        ingredient_id=ingredient_id,
        movement_type=movement_type,
        quantity_direction=quantity_direction,
        start_date=start_date,
        end_date=end_date,
        search=search,
        sort_field=sort_field,
        sort_direction=sort_direction,
    )


@router.get("/stock/{ingredient_id}", dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def get_ingredient_stock(
    request: Request,
    response: Response,
    ingredient_id: UUID
):
    """
    Get current stock for a specific ingredient

    Returns:
    - Current stock level
    - Minimum and maximum stock
    - Unit cost and total value
    - Location and lot information
    """
    return await inventory_service.get_stock_by_ingredient(
        request,
        response,
        ingredient_id
    )


@router.post("/adjustments", dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def create_inventory_adjustment(
    request: Request,
    response: Response
):
    """
    Create a manual inventory adjustment

    Body:
    - ingredient_id: UUID of the ingredient
    - quantity_change: Amount to adjust (positive for increment, negative for decrement)
    - reason: Reason for the adjustment
    - source: Source of the adjustment (default: manual_adjustment)

    Returns:
    - Created adjustment record
    - Updated stock level
    """
    return await inventory_service.create_adjustment(
        request,
        response
    )

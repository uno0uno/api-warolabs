"""
Stations Router
CRUD and toggle endpoints for kitchen station (preparation point) management.

Issue: https://github.com/uno0uno/warocol.com/issues/411
"""
from fastapi import APIRouter, Request
from typing import Optional, List
from uuid import UUID
from pydantic import BaseModel, Field
from app.services import stations_service

router = APIRouter(tags=["Stations"])


class CreateStationRequest(BaseModel):
    name: str = Field(..., max_length=100)
    kitchen_name: Optional[str] = Field(None, max_length=50)
    color: str = Field('#6B7280', max_length=7, pattern=r'^#[0-9A-Fa-f]{6}$')
    alert_threshold_1_min: int = Field(8, ge=1)
    alert_threshold_2_min: int = Field(15, ge=1)
    display_order: int = Field(0, ge=0)


class UpdateStationRequest(BaseModel):
    name: Optional[str] = Field(None, max_length=100)
    kitchen_name: Optional[str] = Field(None, max_length=50)
    color: Optional[str] = Field(None, max_length=7, pattern=r'^#[0-9A-Fa-f]{6}$')
    alert_threshold_1_min: Optional[int] = Field(None, ge=1)
    alert_threshold_2_min: Optional[int] = Field(None, ge=1)
    display_order: Optional[int] = Field(None, ge=0)


class ToggleStationRequest(BaseModel):
    is_active: bool


class ReorderItem(BaseModel):
    id: UUID
    display_order: int = Field(..., ge=0)


class ReorderRequest(BaseModel):
    items: List[ReorderItem] = Field(..., min_length=1)


class SetCategoryStationRequest(BaseModel):
    station_id: Optional[UUID] = None


# IMPORTANT: static paths (/active, /reorder, /categories) are registered BEFORE
# parameterized paths (/{station_id}) to prevent FastAPI from trying to parse
# literal strings as UUIDs.


@router.get("")
async def list_stations_endpoint(request: Request):
    """List all stations for the tenant ordered by display_order."""
    return await stations_service.list_stations(request)


@router.get("/active")
async def list_active_stations_endpoint(request: Request):
    """List only active stations (used by POS header and KDS routing)."""
    return await stations_service.list_active_stations(request)


@router.patch("/reorder")
async def reorder_stations_endpoint(request: Request, body: ReorderRequest):
    """Bulk-update display_order for multiple stations in a single query."""
    return await stations_service.reorder_stations(request, body.items)


@router.post("")
async def create_station_endpoint(request: Request, body: CreateStationRequest):
    """Create a new kitchen station for the tenant."""
    return await stations_service.create_station(request, body)


@router.get("/categories")
async def list_category_stations_endpoint(request: Request):
    """List all category→station assignments for the tenant."""
    return await stations_service.get_category_stations(request)


@router.post("/categories/{category_id}")
async def set_category_station_endpoint(request: Request, category_id: UUID, body: SetCategoryStationRequest):
    """Assign (or clear) a kitchen station for a category (UPSERT). Pass station_id=null to clear."""
    return await stations_service.set_category_station(request, category_id, body.station_id)


@router.delete("/categories/{category_id}")
async def delete_category_station_endpoint(request: Request, category_id: UUID):
    """Remove the station assignment for a category."""
    return await stations_service.delete_category_station(request, category_id)


@router.patch("/{station_id}")
async def update_station_endpoint(request: Request, station_id: UUID, body: UpdateStationRequest):
    """Partial-update a station's name, color, thresholds, or display_order."""
    return await stations_service.update_station(request, station_id, body)


@router.delete("/{station_id}")
async def soft_delete_station_endpoint(request: Request, station_id: UUID):
    """
    Soft-delete a station (is_active = false).
    Returns 409 if the station has active comandas (pending / preparing / ready).
    """
    return await stations_service.soft_delete_station(request, station_id)


@router.patch("/{station_id}/toggle")
async def toggle_station_endpoint(request: Request, station_id: UUID, body: ToggleStationRequest):
    """Toggle is_active on/off for a station (used by POS session toggle)."""
    return await stations_service.toggle_station(request, station_id, body.is_active)

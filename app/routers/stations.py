"""
Stations Router
CRUD and toggle endpoints for kitchen station (preparation point) management.

Issue: https://github.com/uno0uno/warocol.com/issues/411
"""
from fastapi import APIRouter, Request, HTTPException
from typing import Optional, List
from uuid import UUID
from pydantic import BaseModel, Field
from app.services import stations_service
from app.database import get_db_connection

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


@router.get("/{station_id}")
async def get_station_public_endpoint(station_id: UUID):
    """
    Public endpoint — returns station metadata for the KDS screen.
    No session required; the station UUID acts as the access token.
    Issue: https://github.com/uno0uno/warocol.com/issues/422
    """
    async with get_db_connection(use_transaction=False) as conn:
        row = await conn.fetchrow(
            """
            SELECT
                id, name, kitchen_name, color, is_active,
                alert_threshold_1_min, alert_threshold_2_min,
                tenant_id
            FROM kitchen_stations
            WHERE id = $1 AND is_active = true
            """,
            station_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Station not found")

    # Fetch kds_enabled from the tenant profile
    async with get_db_connection(use_transaction=False) as conn:
        kds_enabled = await conn.fetchval(
            "SELECT kds_enabled FROM tenant_public_profiles WHERE tenant_id = $1",
            row["tenant_id"],
        )

    data = dict(row)
    data.pop("tenant_id", None)
    data["kds_enabled"] = bool(kds_enabled)
    return {"success": True, "data": data}


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


@router.get("/{station_id}/deactivate-info")
async def get_deactivate_info(request: Request, station_id: UUID):
    """Return active-comanda count and affected categories before deactivating a station."""
    return await stations_service.get_deactivate_info(request, station_id)


@router.patch("/{station_id}/toggle")
async def toggle_station_endpoint(request: Request, station_id: UUID, body: ToggleStationRequest):
    """Toggle is_active on/off for a station."""
    return await stations_service.toggle_station(request, station_id, body.is_active)


# ── KDS Token Management ────────────────────────────────────────────────────

@router.post("/{station_id}/kds-token")
async def generate_kds_token(request: Request, station_id: UUID):
    """Generate a new KDS access token for a station. Revokes any previous active token."""
    import secrets
    from app.core.middleware import require_valid_session

    session = require_valid_session(request)
    tenant_id = session.tenant_id

    token = secrets.token_urlsafe(36)

    async with get_db_connection() as conn:
        # Revoke any existing active token for this station
        await conn.execute(
            "UPDATE kds_tokens SET revoked_at = now() WHERE station_id = $1 AND revoked_at IS NULL",
            station_id,
        )
        # Create new token
        await conn.execute(
            "INSERT INTO kds_tokens (station_id, tenant_id, token) VALUES ($1, $2, $3)",
            station_id, tenant_id, token,
        )

    return {'success': True, 'data': {'token': token, 'station_id': str(station_id)}}


@router.get("/{station_id}/kds-token")
async def get_kds_token(request: Request, station_id: UUID):
    """Get the current active KDS token for a station.

    Returns the full token so the operator UI can rebuild the KDS URL on
    every page load without forcing a regenerate. The endpoint is
    session-protected, so only authenticated tenant operators can read it.
    """
    from app.core.middleware import require_valid_session

    require_valid_session(request)

    async with get_db_connection() as conn:
        row = await conn.fetchrow(
            "SELECT token, created_at FROM kds_tokens WHERE station_id = $1 AND revoked_at IS NULL ORDER BY created_at DESC LIMIT 1",
            station_id,
        )

    if not row:
        return {'success': True, 'data': None}

    return {
        'success': True,
        'data': {
            'token': row['token'],
            'created_at': row['created_at'].isoformat() if row['created_at'] else None,
        },
    }


@router.delete("/{station_id}/kds-token")
async def revoke_kds_token(request: Request, station_id: UUID):
    """Revoke all active KDS tokens for a station."""
    from app.core.middleware import require_valid_session

    require_valid_session(request)

    async with get_db_connection() as conn:
        await conn.execute(
            "UPDATE kds_tokens SET revoked_at = now() WHERE station_id = $1 AND revoked_at IS NULL",
            station_id,
        )

    return {'success': True, 'message': 'Token revocado'}

"""
Comandas Router — KDS (Kitchen Display System)
Authenticated endpoints for kitchen operators to manage order lifecycle.

Issue: https://github.com/uno0uno/warocol.com/issues/413
Issue: https://github.com/uno0uno/warocol.com/issues/416
"""
from fastapi import APIRouter, Request, Query
from typing import Optional
from uuid import UUID
from app.services import comandas_service
from app.models.comanda import ComandaStatusUpdateRequest, ComandaItemStatusUpdateRequest, BulkComandaStatusUpdateRequest

router = APIRouter(tags=["KDS / Comandas"])


# ─────────────────────────────────────────────────────────────────────────────
# IMPORTANT: static paths (/active, /history, /summary) MUST be registered
# BEFORE parameterized paths (/{comanda_id}) to prevent FastAPI from trying
# to parse literal strings as UUIDs. Same pattern as stations.py.
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/active")
async def list_active_comandas(
    request: Request,
    station_id: Optional[UUID] = Query(None, description="Filter by kitchen station"),
    status: Optional[str] = Query(None, description="Filter by status (pending, preparing, ready)"),
    source_type: Optional[str] = Query(None, description="Filter by source: table | pos | delivery | pickup"),
    date: Optional[str] = Query(None, description="ISO date YYYY-MM-DD, defaults to today"),
):
    """
    List active comandas (pending, preparing, ready) for the tenant.
    Kept for backwards compatibility — prefer GET /api/comandas (root).
    """
    return await comandas_service.get_comandas_for_kds(
        request, station_id, status, date, source_type
    )


@router.get("/history")
async def list_comanda_history(
    request: Request,
    date_from: Optional[str] = Query(None, description="ISO format date from"),
    date_to: Optional[str] = Query(None, description="ISO format date to"),
    station_id: Optional[UUID] = Query(None),
    status: Optional[str] = Query(None),
    source_type: Optional[str] = Query(None, description="Filter by source: table | pos | delivery | pickup"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0)
):
    """
    List all historical comandas with pagination and filters.
    """
    return await comandas_service.get_comanda_history(
        request, date_from, date_to, station_id, status, source_type, limit, offset
    )


@router.get("/summary")
async def get_comanda_summary(
    request: Request,
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None)
):
    """
    Get aggregated performance stats per station.
    """
    return await comandas_service.get_comanda_summary(request, date_from, date_to)


@router.get("/stats")
async def get_stats_endpoint(
    request: Request,
    date: Optional[str] = Query(None, description="ISO date YYYY-MM-DD, defaults to today"),
    station_id: Optional[UUID] = Query(None, description="Filter to a single station"),
):
    """
    Daily performance stats per kitchen station.
    Returns total/delivered/cancelled counts, avg prep time, delay counts, and source breakdown.
    """
    return await comandas_service.get_daily_stats(request, date, station_id)


@router.get("")
async def list_comandas(
    request: Request,
    station_id: Optional[UUID] = Query(None, description="Filter by kitchen station (KDS screen mode)"),
    status: Optional[str] = Query(None, description="Filter by status; defaults to pending,preparing,ready"),
    source_type: Optional[str] = Query(None, description="Filter by source: table | pos | delivery | pickup"),
    date: Optional[str] = Query(None, description="ISO date YYYY-MM-DD, defaults to today"),
):
    """
    List active comandas for the tenant.
    Without station_id: Expo/monitor view (all stations).
    With station_id: KDS view for that specific station.
    Computes elapsed_seconds and alert_level server-side.
    Polling interval recommendation: every 5s from frontend.
    """
    return await comandas_service.get_comandas_for_kds(
        request, station_id, status, date, source_type
    )


@router.patch("/bulk-status")
async def bulk_update_comanda_status(
    request: Request,
    body: BulkComandaStatusUpdateRequest,
):
    """
    Bulk status update for multiple comandas.
    Applies the same transition rules per comanda — skips invalid transitions.
    Returns counts of updated and skipped.
    """
    return await comandas_service.bulk_update_comanda_status(request, body.comanda_ids, body.status)


@router.get("/{comanda_id}")
async def get_comanda_detail(
    request: Request,
    comanda_id: UUID,
):
    """
    Get full comanda detail including nested items, station info, timing,
    elapsed_seconds, and alert_level.
    """
    return await comandas_service.get_comanda_detail(request, comanda_id)


@router.patch("/{comanda_id}/status")
async def update_comanda_status(
    request: Request,
    comanda_id: UUID,
    body: ComandaStatusUpdateRequest,
):
    """
    Advance comanda status. Enforces allowed transitions:
    - pending → preparing | cancelled
    - preparing → ready | cancelled
    - ready → delivered
    Sets appropriate timestamps (preparing_at, ready_at, delivered_at).
    Returns 422 for illegal transitions.
    """
    return await comandas_service.update_comanda_status(request, comanda_id, body.status)


@router.post("/{comanda_id}/deliver")
async def deliver_comanda(
    request: Request,
    comanda_id: UUID
):
    """
    Mark comanda as delivered. Removes it from active KDS views.
    Shortcut for PATCH /status with { "status": "delivered" }.
    """
    return await comandas_service.update_comanda_status(request, comanda_id, 'delivered')


@router.post("/{comanda_id}/recall")
async def recall_comanda(
    request: Request,
    comanda_id: UUID
):
    """
    Undo delivery and return comanda to 'ready' status.
    Allowed only within a 15-minute window after delivery.
    """
    return await comandas_service.recall_comanda(request, comanda_id)


@router.patch("/{comanda_id}/items/{item_id}/status")
async def update_comanda_item_status(
    request: Request,
    comanda_id: UUID,
    item_id: UUID,
    body: ComandaItemStatusUpdateRequest,
):
    """
    Bump an individual item within a comanda to 'ready'.

    Side effects (atomic):
    - Sets comanda_items.status = 'ready', ready_at = now()
    - Sets order_items.fulfillment_status = 'ready', ready_at = now()
    - If ALL items in comanda are now ready → auto-advances comanda to 'ready'
    """
    return await comandas_service.update_comanda_item_status(
        request, comanda_id, item_id, body.status
    )

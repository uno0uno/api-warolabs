"""
Analytics Router
Endpoints for analytics dashboard data
"""
from fastapi import APIRouter, Depends, Request, Query
from typing import Optional
from uuid import UUID
from app.core.permissions import Module, require_module
from app.services import analytics_service
from app.models.data_quality import DataQualityAlertResolve

router = APIRouter(prefix="/analytics", tags=["Analytics"])


@router.get("/menu-analysis", dependencies=[Depends(require_module(Module.ANALITICA))])
async def get_menu_analysis(
    request: Request,
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=500)
):
    """
    Get menu analysis with profitability and popularity matrix

    Returns products classified as:
    - Stars: High profit, high popularity
    - Plowhorses: Low profit, high popularity
    - Puzzles: High profit, low popularity
    - Dogs: Low profit, low popularity

    Query parameters:
    - date_from: Start date (YYYY-MM-DD)
    - date_to: End date (YYYY-MM-DD)
    - limit: Max number of products to return
    """
    return await analytics_service.get_menu_analysis(
        request,
        date_from=date_from,
        date_to=date_to,
        limit=limit
    )


@router.get("/food-cost", dependencies=[Depends(require_module(Module.ANALITICA))])
async def get_food_cost(
    request: Request,
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None)
):
    """
    Get food cost percentage with month-over-month comparison

    Query parameters:
    - date_from: Start date (YYYY-MM-DD)
    - date_to: End date (YYYY-MM-DD)
    """
    return await analytics_service.get_food_cost(
        request,
        date_from=date_from,
        date_to=date_to
    )


@router.get("/alerts", dependencies=[Depends(require_module(Module.ANALITICA))])
async def get_alerts(
    request: Request,
    limit: int = Query(10, ge=1, le=50)
):
    """
    Get system alerts for inventory, expiration warnings, and other notifications

    Returns alerts for:
    - Low stock items
    - Out of stock items
    - Expiring ingredients
    - Top selling products (for reordering)

    Query parameters:
    - limit: Max number of alerts to return
    """
    return await analytics_service.get_alerts(
        request,
        limit=limit
    )


@router.get("/data-quality", dependencies=[Depends(require_module(Module.ANALITICA))])
async def get_data_quality(request: Request):
    """
    Scan 30-day purchase history for price anomalies and return quality score.

    Detects:
    - price_spike / price_drop: >25% deviation from rolling average (warning),
      >50% or outside IQR×2 fence (critical)
    - impossible_value: unit_cost <= 0 (critical)

    Returns:
    - score: 0-100 quality score (100 - critical*10 - warning*2)
    - critical / warning / resolved counts
    - alerts: full list ordered by severity and date
    """
    return await analytics_service.get_data_quality(request)


@router.patch("/data-quality/{alert_id}/resolve", dependencies=[Depends(require_module(Module.ANALITICA))])
async def resolve_data_quality_alert(
    alert_id: UUID,
    resolve_data: DataQualityAlertResolve,
    request: Request,
):
    """
    Resolve a data quality alert.

    Body:
    - resolution_type: "valid" | "corrected"
    - corrected_value: float (required if resolution_type = "corrected")
    - corrected_quantity: float (optional — corrects purchase_quantity too)
    - resolution_note: str (optional)

    Modes:
    - valid: marks alert resolved, no changes to purchase data
    - corrected: updates unit_cost (and optionally purchase_quantity) on the
      purchase item, recalculates costo_calculado on all affected products,
      saves original_value + corrected_value as audit log

    Returns 400 if the corrected value is itself anomalous vs prior history.
    Returns 400 if alert is already resolved.
    Returns 404 if alert not found for this tenant.
    """
    return await analytics_service.resolve_data_quality_alert(
        request, alert_id, resolve_data
    )


@router.get("/kitchen", dependencies=[Depends(require_module(Module.ANALITICA))])
async def get_kitchen_metrics(
    request: Request,
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None)
):
    """
    Get kitchen performance metrics: avg prep time, station load, and late orders.

    Query parameters:
    - date_from: Start date (YYYY-MM-DD)
    - date_to: End date (YYYY-MM-DD)
    """
    return await analytics_service.get_kitchen_metrics(
        request,
        date_from=date_from,
        date_to=date_to
    )

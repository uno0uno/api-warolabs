"""
Analytics Router
Endpoints for analytics dashboard data
"""
from fastapi import APIRouter, Request, Query
from typing import Optional
from app.services import analytics_service

router = APIRouter(prefix="/analytics", tags=["Analytics"])


@router.get("/menu-analysis")
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


@router.get("/food-cost")
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


@router.get("/alerts")
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

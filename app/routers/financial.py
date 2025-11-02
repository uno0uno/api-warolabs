from fastapi import APIRouter, Request, Response, Query
from datetime import datetime
from typing import Optional
from app.services.financial_service import get_tir_metrics, get_products_analysis, get_obstacles_analysis
from app.models.financial import TirMetricsResponse

router = APIRouter()

@router.get("/tir-metrics", response_model=TirMetricsResponse)
async def get_tir_metrics_endpoint(
    request: Request, 
    response: Response,
    period: str = Query(default="monthly", description="Period type for metrics"),
    limit: int = Query(default=12, description="Limit number of records")
):
    """
    Get TIR metrics for financial analysis
    """
    result = await get_tir_metrics(request, response, period, limit)
    
    return TirMetricsResponse(
        data=result,
        timestamp=datetime.now()
    )

@router.get("/products-analysis")
async def products_analysis_endpoint(
    request: Request, 
    response: Response,
    period: int = Query(default=365, description="Period in days for analysis"),
    category: Optional[str] = Query(default=None, description="Filter by category"),
    min_margin: Optional[int] = Query(default=None, description="Minimum margin percentage"),
    sort_by: str = Query(default="margin", description="Sort by: margin, sales, profit, impact")
):
    """
    Get product sales and profitability analysis
    """
    result = await get_products_analysis(request, response, period, category, min_margin, sort_by)
    
    return {
        "success": True,
        "data": result,
        "timestamp": datetime.now().isoformat()
    }

@router.get("/obstacles-analysis")
async def obstacles_analysis_endpoint(
    request: Request, 
    response: Response,
    period: int = Query(default=30, description="Period in days for analysis")
):
    """
    Get business operational obstacles analysis
    """
    result = await get_obstacles_analysis(request, response, period)
    
    return {
        "success": True,
        "data": result,
        "timestamp": datetime.now().isoformat()
    }
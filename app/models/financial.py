from pydantic import BaseModel, Field
from datetime import datetime
from typing import List, Optional, Dict, Any

class TirCurrentMetrics(BaseModel):
    tir_actual: float
    tir_projected: float
    tir_target: float
    recovery_months: float
    total_revenue: float
    gross_profit: float

class TirChartData(BaseModel):
    labels: List[str]
    actualTir: List[float] = Field(alias='actual_tir')
    projectedTir: List[float] = Field(alias='projected_tir')
    class Config:
        from_attributes = True
        populate_by_name = True

class TirTableRow(BaseModel):
    month: str
    tir: float
    investment: float
    monthlyRevenue: float = Field(alias='monthly_revenue')
    return_amount: float = Field(alias='return')
    
    class Config:
        populate_by_name = True

class TirTableTotals(BaseModel):
    tir_average: float
    total_investment: float
    total_revenue: float
    total_return: float
    months_count: int

class TirTableData(BaseModel):
    actual: List[TirTableRow]
    projected: List[TirTableRow]
    totals: Dict[str, TirTableTotals]

class TirMetricsResponse(BaseModel):
    success: bool = True
    data: Dict[str, Any]
    timestamp: datetime
    
class ProductAnalysisResponse(BaseModel):
    success: bool = True
    data: Dict[str, Any]
    timestamp: datetime

class ObstacleAnalysisResponse(BaseModel):
    success: bool = True
    data: Dict[str, Any]
    timestamp: datetime
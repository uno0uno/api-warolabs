from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from uuid import UUID
from datetime import datetime
from enum import Enum


class AlertType(str, Enum):
    price_spike = "price_spike"
    price_drop = "price_drop"
    impossible_value = "impossible_value"
    unit_mismatch = "unit_mismatch"


class AlertSeverity(str, Enum):
    critical = "critical"
    warning = "warning"


class DataQualityAlertDB(BaseModel):
    id: UUID
    tenant_id: UUID
    purchase_item_id: Optional[UUID] = None
    ingredient_id: Optional[UUID] = None
    ingredient_name: str
    alert_type: AlertType
    severity: AlertSeverity
    expected_value: Optional[float] = None
    actual_value: Optional[float] = None
    deviation_pct: Optional[float] = None
    rolling_avg: Optional[float] = None
    context: Optional[Dict[str, Any]] = None
    resolved: bool
    resolved_by: Optional[UUID] = None
    resolved_at: Optional[datetime] = None
    resolution_note: Optional[str] = None
    original_value: Optional[float] = None
    corrected_value: Optional[float] = None
    created_at: datetime


class DataQualityAlertResolve(BaseModel):
    resolution_type: str  # "corrected" | "valid"
    corrected_value: Optional[float] = None
    corrected_quantity: Optional[float] = None
    resolution_note: Optional[str] = None


class DataQualityAlertSummary(BaseModel):
    score: int
    critical: int
    warning: int
    resolved: int
    alerts: List[DataQualityAlertDB]

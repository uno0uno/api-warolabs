"""
Comanda models — KDS (Kitchen Display System)

Pydantic models for fire_comandas() engine and comanda management.

Issue: https://github.com/uno0uno/warocol.com/issues/413
Issue: https://github.com/uno0uno/warocol.com/issues/416
"""
from pydantic import BaseModel, Field
from typing import Optional, List, Any, Dict, Literal
from uuid import UUID
from datetime import datetime
from decimal import Decimal


class ComandaItem(BaseModel):
    id: UUID
    order_item_id: UUID
    kitchen_name: str
    quantity: Decimal
    notes: Optional[str] = None
    modifiers_snapshot: Optional[List[Dict[str, Any]]] = None
    status: str
    ready_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


class Comanda(BaseModel):
    id: UUID
    comanda_number: int
    station_id: Optional[UUID] = None
    station_name: Optional[str] = None
    station_kitchen_name: Optional[str] = None
    station_color: Optional[str] = None
    status: str
    source_type: str
    table_display_name: Optional[str] = None
    notes: Optional[str] = None
    fired_at: datetime
    preparing_at: Optional[datetime] = None
    ready_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    created_at: datetime
    elapsed_seconds: Optional[int] = None
    alert_level: Optional[int] = None
    items: List[ComandaItem] = []

    class Config:
        from_attributes = True


class FireComandasResult(BaseModel):
    success: bool = True
    comandas: List[Comanda] = []
    fired_count: int = 0
    skipped_count: int = 0


class ComandaStatusUpdateRequest(BaseModel):
    """
    Request body for PATCH /{comanda_id}/status.
    'pending' is excluded — you can never manually revert to pending.
    Use POST /recall to move delivered → ready.
    """
    status: Literal['preparing', 'ready', 'delivered', 'cancelled']


class BulkComandaStatusUpdateRequest(BaseModel):
    """Request body for PATCH /bulk-status."""
    comanda_ids: List[UUID]
    status: Literal['preparing', 'ready', 'delivered', 'cancelled']


class ComandaItemStatusUpdateRequest(BaseModel):
    """
    Request body for PATCH /{comanda_id}/items/{item_id}/status.
    Items move pending → ready (kitchen done) or any non-terminal → cancelled (voided from POS).
    """
    status: Literal['ready', 'cancelled']


class StationInfo(BaseModel):
    id: UUID
    name: str
    color: Optional[str] = None
    kitchen_name: Optional[str] = None


class BySource(BaseModel):
    table: int = 0
    pos: int = 0
    delivery: int = 0
    pickup: int = 0


class StationStats(BaseModel):
    station: StationInfo
    total_count: int = 0
    delivered_count: int = 0
    cancelled_count: int = 0
    avg_prep_time_seconds: Optional[float] = None
    delayed_count: int = 0
    very_delayed_count: int = 0
    by_source: BySource = Field(default_factory=BySource)


class ComandaStatsResponse(BaseModel):
    date: str
    stations: List[StationStats] = []

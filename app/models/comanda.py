"""
Comanda models — KDS (Kitchen Display System)

Pydantic models for fire_comandas() engine and comanda management.

Issue: https://github.com/uno0uno/warocol.com/issues/413
"""
from pydantic import BaseModel
from typing import Optional, List, Any, Dict
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
    station_id: UUID
    station_name: Optional[str] = None
    status: str
    source_type: str
    table_display_name: Optional[str] = None
    notes: Optional[str] = None
    fired_at: datetime
    ready_at: Optional[datetime] = None
    created_at: datetime
    items: List[ComandaItem] = []

    class Config:
        from_attributes = True


class FireComandasResult(BaseModel):
    success: bool = True
    comandas: List[Comanda] = []
    fired_count: int = 0
    skipped_count: int = 0

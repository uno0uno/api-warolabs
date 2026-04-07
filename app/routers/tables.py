"""
Tables Router
CRUD and session lifecycle endpoints for restaurant table management.

Issue: https://github.com/uno0uno/warocol.com/issues/298
"""
from fastapi import APIRouter, Request
from typing import Optional, List
from uuid import UUID
from pydantic import BaseModel, Field
from app.services import tables_service

router = APIRouter(tags=["Tables"])


class CreateTableRequest(BaseModel):
    name: str = Field(..., max_length=50, description="e.g. 'Mesa 1', 'Barra 2'")
    capacity: Optional[int] = Field(None, gt=0)


class UpdateTableRequest(BaseModel):
    name: Optional[str] = Field(None, max_length=50)
    capacity: Optional[int] = Field(None, gt=0)


class TabModifier(BaseModel):
    id: Optional[str] = None
    name: Optional[str] = None
    price: float = 0.0


class TabItem(BaseModel):
    product_id: str
    quantity: int = Field(..., gt=0)
    unit_price: float = Field(..., ge=0)
    modifiers: Optional[List[TabModifier]] = None
    notes: Optional[str] = None


class TabAddRequest(BaseModel):
    items: List[TabItem] = Field(..., min_length=1)


class UpdateTabItemRequest(BaseModel):
    quantity: int = Field(..., ge=1)


@router.get("")
async def list_tables(request: Request):
    """
    List all active tables for the tenant.
    Each table includes current status, open session duration (minutes),
    and running total from linked orders.
    """
    return await tables_service.list_tables(request)


@router.post("")
async def create_table(request: Request, body: CreateTableRequest):
    """
    Create a new table for the tenant.
    """
    return await tables_service.create_table(request, body.name, body.capacity)


@router.put("/{table_id}")
async def update_table(request: Request, table_id: UUID, body: UpdateTableRequest):
    """
    Update a table's name and/or capacity.
    Status is NOT editable here — use session endpoints to change status.
    """
    return await tables_service.update_table(request, table_id, body.name, body.capacity)


@router.delete("/{table_id}")
async def soft_delete_table(request: Request, table_id: UUID):
    """
    Soft-delete a table (is_active = false).
    Returns 409 if the table has an open session.
    """
    return await tables_service.soft_delete_table(request, table_id)


@router.post("/{table_id}/open")
async def open_session(request: Request, table_id: UUID):
    """
    Open a new session for a table (status: free → open).
    Returns 409 if a session is already open.
    """
    return await tables_service.open_session(request, table_id)


@router.post("/{table_id}/close")
async def close_session(request: Request, table_id: UUID):
    """
    Close the active session (status: open/bill_requested → free).
    Pending orders on this session remain pending — frontend routes to payment.
    Returns 404 if no open session exists.
    """
    return await tables_service.close_session(request, table_id)


@router.get("/{table_id}/current")
async def get_current_session(request: Request, table_id: UUID):
    """
    Get the open session for a table with all linked orders and running total.
    Returns 404 if no open session exists.
    """
    return await tables_service.get_current_session(request, table_id)


@router.post("/{table_id}/tab/add")
async def add_tab_items(request: Request, table_id: UUID, body: TabAddRequest):
    """
    Add items to the running tab for a table session.
    Creates a pending order linked to the table_session_id.
    Returns 404 if no open session exists.
    """
    items = [
        {
            "product_id": item.product_id,
            "quantity": item.quantity,
            "unit_price": item.unit_price,
            "modifiers": [
                {"id": m.id, "name": m.name, "price": m.price}
                for m in (item.modifiers or [])
            ],
            "notes": item.notes,
        }
        for item in body.items
    ]
    return await tables_service.add_tab_items(request, table_id, items)


@router.delete("/{table_id}/tab/items/{order_item_id}")
async def remove_tab_item(request: Request, table_id: UUID, order_item_id: UUID):
    """Remove an order item from the running tab."""
    return await tables_service.remove_tab_item(request, table_id, order_item_id)


@router.patch("/{table_id}/tab/items/{order_item_id}")
async def update_tab_item_quantity(request: Request, table_id: UUID, order_item_id: UUID, body: UpdateTabItemRequest):
    """Update the quantity of an order item in the running tab."""
    return await tables_service.update_tab_item_quantity(request, table_id, order_item_id, body.quantity)


@router.post("/{table_id}/bill")
async def request_bill(request: Request, table_id: UUID):
    """
    Mark a table as bill_requested (status: open → bill_requested).
    Returns 409 if table is not open.
    """
    return await tables_service.request_bill(request, table_id)

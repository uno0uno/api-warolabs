"""
Tables Router
CRUD and session lifecycle endpoints for restaurant table management.

Issue: https://github.com/uno0uno/warocol.com/issues/298
"""
from fastapi import APIRouter, Depends, Request, Query
from typing import Optional, List
from uuid import UUID
from datetime import date
from pydantic import BaseModel, Field
from app.core.permissions import Module, require_module
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


@router.get("", dependencies=[Depends(require_module(Module.POS))])
async def list_tables(request: Request, include_inactive: bool = Query(False)):
    """
    List tables for the tenant.
    include_inactive=true (admin): also returns deactivated tables (is_active=false, deleted_at IS NULL).
    Each table includes current status, session duration, running total, and has_history flag.
    """
    return await tables_service.list_tables(request, include_inactive=include_inactive)


@router.post("", dependencies=[Depends(require_module(Module.POS))])
async def create_table(request: Request, body: CreateTableRequest):
    """
    Create a new table for the tenant.
    """
    return await tables_service.create_table(request, body.name, body.capacity)


@router.put("/{table_id}", dependencies=[Depends(require_module(Module.POS))])
async def update_table(request: Request, table_id: UUID, body: UpdateTableRequest):
    """
    Update a table's name and/or capacity.
    Status is NOT editable here — use session endpoints to change status.
    """
    return await tables_service.update_table(request, table_id, body.name, body.capacity)


@router.patch("/{table_id}/activate", dependencies=[Depends(require_module(Module.POS))])
async def activate_table(request: Request, table_id: UUID):
    """
    Re-activate a deactivated table (is_active = true).
    Returns 409 if table is permanently deleted or is bar.
    Issue: https://github.com/uno0uno/warocol.com/issues/436
    """
    return await tables_service.activate_table(request, table_id)


@router.patch("/{table_id}/deactivate", dependencies=[Depends(require_module(Module.POS))])
async def deactivate_table(request: Request, table_id: UUID):
    """
    Temporarily deactivate a table (is_active = false).
    Returns 409 if table has an open session or is bar.
    Issue: https://github.com/uno0uno/warocol.com/issues/436
    """
    return await tables_service.deactivate_table(request, table_id)


@router.delete("/{table_id}", dependencies=[Depends(require_module(Module.POS))])
async def delete_table_permanent(request: Request, table_id: UUID):
    """
    Permanently remove a table.
    - Open session → 409
    - No history → hard DELETE from DB
    - Has history → soft-archive (deleted_at = now(), preserves reporting data)
    Issue: https://github.com/uno0uno/warocol.com/issues/436
    """
    return await tables_service.delete_table_permanent(request, table_id)


@router.post("/{table_id}/open", dependencies=[Depends(require_module(Module.POS))])
async def open_session(request: Request, table_id: UUID):
    """
    Open a new session for a table (status: free → open).
    Returns 409 if a session is already open.
    """
    return await tables_service.open_session(request, table_id)


class CloseSessionRequest(BaseModel):
    payment_method: Optional[str] = Field(None, description="cash | card | digital | credit — marks pending orders as completed")
    customer_id: Optional[str] = Field(None, description="Customer UUID to associate with completed orders")
    credit_due_date: Optional[date] = Field(None, description="Optional due date for credit orders (only used when payment_method='credit')")
    payment_method_id: Optional[UUID] = Field(None, description="UUID of the selected payment_methods row (nullable if group-level only)")
    discount_type: Optional[str] = Field(None, description="'percent' | 'fixed'")
    discount_value: Optional[float] = Field(None, description="10 for 10%, 5000 for $5,000 COP")
    split_mode: bool = Field(False, description="True when using split payment — keeps session open, marks orders as partial")
    split_first_amount: float = Field(0.0, description="Amount for the first split payment (used only when split_mode=True)")
    split_first_cash_received: Optional[float] = Field(None, description="Issue #524 — cash handed over for the first split payment when payment_method='cash'. Must be >= split_first_amount.")
    cash_received: Optional[float] = Field(None, description="Issue #524 — cash handed over for a single (non-split) cash close. Must be >= total session amount.")


class AddSessionPaymentRequest(BaseModel):
    amount: float = Field(..., description="Amount for this partial payment")
    payment_method: str = Field(..., description="cash | card | digital")
    payment_method_id: Optional[UUID] = Field(None, description="UUID of the selected payment_methods row")
    cash_received: Optional[float] = Field(None, description="Issue #524 — cash handed over for this payment when payment_method='cash'. Must be >= amount.")


@router.post("/{table_id}/close", dependencies=[Depends(require_module(Module.POS))])
async def close_session(request: Request, table_id: UUID, body: CloseSessionRequest = CloseSessionRequest()):
    """
    Close the active session (status: open/bill_requested → free).
    If payment_method is provided, all pending orders are marked as completed.
    If split_mode=True, marks orders as partial and records first payment without closing the session.
    Returns 404 if no open session exists.

    Response data includes:
    - session_id, table_id
    - completed_orders, pending_orders
    - order_ids: UUID[] of completed orders
    - order_numbers: int[] aligned with order_ids
    - order_number: int (only when a single order was completed)
    """
    return await tables_service.close_session(
        request, table_id,
        body.payment_method, body.customer_id,
        body.credit_due_date, body.payment_method_id,
        body.discount_type, body.discount_value,
        body.split_mode, body.split_first_amount,
        split_first_cash_received=body.split_first_cash_received,
        cash_received=body.cash_received,
    )


@router.post("/{table_id}/payments", dependencies=[Depends(require_module(Module.POS))])
async def add_session_payment(request: Request, table_id: UUID, body: AddSessionPaymentRequest):
    """
    Add a partial payment to an open mesa session's split payment.
    When total paid >= session total, closes the session automatically.
    Returns 404 if no open session or no partial orders exist.
    """
    return await tables_service.add_session_payment(
        request, table_id,
        body.amount, body.payment_method, body.payment_method_id,
        cash_received=body.cash_received,
    )


@router.get("/{table_id}/current", dependencies=[Depends(require_module(Module.POS))])
async def get_current_session(request: Request, table_id: UUID):
    """
    Get the open session for a table with all linked orders and running total.
    Returns 404 if no open session exists.
    """
    return await tables_service.get_current_session(request, table_id)


@router.post("/{table_id}/tab/add", dependencies=[Depends(require_module(Module.POS))])
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


@router.delete("/{table_id}/tab/items/{order_item_id}", dependencies=[Depends(require_module(Module.POS))])
async def remove_tab_item(request: Request, table_id: UUID, order_item_id: UUID):
    """Remove an order item from the running tab."""
    return await tables_service.remove_tab_item(request, table_id, order_item_id)


@router.patch("/{table_id}/tab/items/{order_item_id}", dependencies=[Depends(require_module(Module.POS))])
async def update_tab_item_quantity(request: Request, table_id: UUID, order_item_id: UUID, body: UpdateTabItemRequest):
    """Update the quantity of an order item in the running tab."""
    return await tables_service.update_tab_item_quantity(request, table_id, order_item_id, body.quantity)


@router.delete("/{table_id}/tab", dependencies=[Depends(require_module(Module.POS))])
async def clear_tab(request: Request, table_id: UUID):
    """
    Delete all pending orders for the active session without closing it.
    The table stays open and ready for new orders.
    """
    return await tables_service.clear_tab(request, table_id)


@router.post("/{table_id}/bill", dependencies=[Depends(require_module(Module.POS))])
async def request_bill(request: Request, table_id: UUID):
    """
    Mark a table as bill_requested (status: open → bill_requested).
    Returns 409 if table is not open.
    """
    return await tables_service.request_bill(request, table_id)


@router.post("/{table_id}/fire", dependencies=[Depends(require_module(Module.POS))])
async def fire_table_items(request: Request, table_id: UUID):
    """
    Explicitly fire all 'new' items in the table session to the kitchen.
    Returns comanda summaries and fired item count.
    """
    return await tables_service.fire_table_items(request, table_id)


@router.delete("/{table_id}/session", dependencies=[Depends(require_module(Module.POS))])
async def discard_session(request: Request, table_id: UUID):
    """
    Discard the active session: hard-delete all pending orders/items,
    soft-close the session (is_discarded=TRUE, closed_at=now()), reset table to free.
    Returns 404 if no open session. Returns 409 for bar table or completed orders.

    Issue: https://github.com/uno0uno/warocol.com/issues/337
    """
    return await tables_service.discard_table_session(request, table_id)


@router.post("/{table_id}/session/reopen", dependencies=[Depends(require_module(Module.POS))])
async def reopen_session(request: Request, table_id: UUID):
    """
    Reopen the most recent non-discarded closed session for a table.
    Returns 409 for bar table or if table already has an open session.
    Returns 404 if no closed session exists to reopen.

    Issue: https://github.com/uno0uno/warocol.com/issues/337
    """
    return await tables_service.reopen_table_session(request, table_id)


class MoveTableRequest(BaseModel):
    target_table_id: UUID


@router.post("/{source_table_id}/move", dependencies=[Depends(require_module(Module.POS))])
async def move_table_session(request: Request, source_table_id: UUID, body: MoveTableRequest):
    """
    Transfer all pending orders from source table's open session to target table.
    - Source session is closed; target gets a new session.
    - Returns 400 if source == target.
    - Returns 404 if source has no open session.
    - Returns 409 if source is a bar table, or target is occupied.

    Issue: https://github.com/uno0uno/warocol.com/issues/314
    """
    return await tables_service.move_table_session(request, source_table_id, body.target_table_id)

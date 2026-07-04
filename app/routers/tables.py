"""
Tables Router
CRUD and session lifecycle endpoints for restaurant table management.

Issue: https://github.com/uno0uno/warocol.com/issues/298
"""
from fastapi import APIRouter, Body, Depends, Request, Query
from typing import Optional, List, Literal
from uuid import UUID
from datetime import date
from decimal import Decimal
from pydantic import BaseModel, Field
from app.core.permissions import Module, require_module
from app.models.table_member_assignment import OpenTableRequest
from app.models.comanda import FireTableItemsRequest
from app.services import table_session_advances_service, tables_service

router = APIRouter(tags=["Tables"])


class CreateTableRequest(BaseModel):
    name: str = Field(..., max_length=50, description="e.g. 'Mesa 1', 'Barra 2'")
    capacity: Optional[int] = Field(None, gt=0)
    code: Optional[str] = Field(
        None,
        max_length=4,
        description="Short POS code (1–4 alphanumeric). Inferred from name when omitted.",
    )


class UpdateTableRequest(BaseModel):
    name: Optional[str] = Field(None, max_length=50)
    capacity: Optional[int] = Field(None, gt=0)
    code: Optional[str] = Field(
        None,
        max_length=4,
        description="Short POS code. Send empty string to re-infer from name.",
    )


class TabModifier(BaseModel):
    id: Optional[str] = None
    name: Optional[str] = None
    price: float = 0.0
    quantity: float = Field(default=1.0, gt=0)


class TabItem(BaseModel):
    product_id: str
    quantity: int = Field(..., gt=0)
    unit_price: float = Field(..., ge=0)
    modifiers: Optional[List[TabModifier]] = None
    notes: Optional[str] = None


class TabAddRequest(BaseModel):
    items: List[TabItem] = Field(..., min_length=1)


class TabDeferDeliveryPaymentRequest(BaseModel):
    customer_id: UUID = Field(..., description="Identified customer that owns the delivery address")
    delivery_address_id: UUID = Field(..., description="Delivery address owned by customer_id")
    delivery_instructions: Optional[str] = Field(None, description="Optional delivery notes")


class UpdateTabItemRequest(BaseModel):
    quantity: int = Field(..., ge=1)
    reason: Optional[str] = Field(
        None,
        description="warocol.com#786 — required when decreasing qty of a fired tab line",
    )


class TabItemContentModifier(BaseModel):
    id: Optional[str] = None
    name: str
    price: float = Field(..., ge=0)
    quantity: int = Field(1, ge=1)


class UpdateTabItemContentRequest(BaseModel):
    modifiers: List[TabItemContentModifier] = Field(default_factory=list)
    notes: Optional[str] = None


class TableQrToggleRequest(BaseModel):
    enabled: bool


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
    return await tables_service.create_table(request, body.name, body.capacity, body.code)


@router.put("/{table_id}", dependencies=[Depends(require_module(Module.POS))])
async def update_table(request: Request, table_id: UUID, body: UpdateTableRequest):
    """
    Update a table's name and/or capacity.
    Status is NOT editable here — use session endpoints to change status.
    """
    return await tables_service.update_table(request, table_id, body.model_dump(exclude_unset=True))


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


@router.patch("/{table_id}/qr", dependencies=[Depends(require_module(Module.POS))])
async def set_table_qr_enabled(request: Request, table_id: UUID, body: TableQrToggleRequest):
    """
    Enable/disable per-table QR ordering. Generates public token on first enable.
    Issue: https://github.com/uno0uno/warocol.com/issues/976 (api-warolabs#266)
    """
    return await tables_service.set_table_qr_enabled(request, table_id, body.enabled)


@router.post("/{table_id}/qr-token/regenerate", dependencies=[Depends(require_module(Module.POS))])
async def regenerate_table_qr_token(request: Request, table_id: UUID):
    """
    Issue a new public QR token; previous printed codes stop resolving.
    Issue: https://github.com/uno0uno/warocol.com/issues/976 (api-warolabs#266)
    """
    return await tables_service.regenerate_table_qr_token(request, table_id)


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
async def open_session(
    request: Request,
    table_id: UUID,
    body: Optional[OpenTableRequest] = None,
):
    """
    Open a new session for a table (status: free → open).
    Returns 409 if a session is already open.

    Optional body (warocol.com#574) lets the cashier pre-set the
    session-level waiter override (`attended_by_member_id`). When absent
    OR when the tenant's `waiter_attribution_enabled` flag is off, the
    session is created with NULL and the resolver inherits from
    `tables.assigned_member_id`. Backward-compatible — clients that send
    no body work as before.
    """
    attended_by = body.attended_by_member_id if body else None
    return await tables_service.open_session(request, table_id, attended_by)


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
    cash_received: Optional[float] = Field(None, description="Issue #524 — cash handed over for a single (non-split) cash close. Must be >= amount due after table-session advances.")
    tip_amount: float = Field(0, ge=0, description="warocol.com#639 — total tip for the mesa session. Applied to the first completed order in the session (like cash_received). Rejected when tip_enabled=false. Allowed with split_mode.")
    tip_source: Literal['preset', 'custom', 'none'] = Field('none', description="warocol.com#639 — how the tip was chosen. Must agree with tip_amount.")
    tip_taxable: bool = Field(False, description="warocol.com#740 — apply consumption tax to tip_amount when true (gravada).")
    served_by_member_id: Optional[UUID] = Field(None, description="warocol.com#663 — waiter assigned at checkout. Applied to all completed orders in the session.")
    reason: Optional[str] = Field(
        None,
        description="Motivo de liberación — required when closing without payment and pending tab lines exist",
    )
    waros_to_redeem: Optional[int] = Field(None, ge=0, description="B1 WaRos to redeem (api#370)")
    waro_reward_id: Optional[UUID] = Field(None, description="B2 reward UUID (api#370)")


class AddSessionPaymentRequest(BaseModel):
    amount: float = Field(..., description="Amount for this partial payment")
    payment_method: str = Field(..., description="cash | card | digital")
    payment_method_id: Optional[UUID] = Field(None, description="UUID of the selected payment_methods row")
    cash_received: Optional[float] = Field(None, description="Issue #524 — cash handed over for this payment when payment_method='cash'. Must be >= amount.")
    tip_amount: Optional[float] = Field(None, ge=0, description="Optional session tip override during follow-up split settlement. Persists on the first completed order header.")
    tip_source: Optional[Literal['preset', 'custom', 'none']] = Field(None, description="Optional tip source patch paired with tip_amount for split session updates.")
    tip_taxable: Optional[bool] = Field(None, description="Optional gravada flag for split session tip updates.")


class CreateSessionAdvanceRequest(BaseModel):
    amount_cop: Decimal = Field(..., gt=0, description="COP amount for this session advance")
    payment_method: str = Field(..., description="cash | card | digital")
    payment_method_id: Optional[UUID] = Field(None, description="UUID of the selected payment_methods row")
    notes: Optional[str] = Field(None, max_length=500)
    idempotency_key: Optional[str] = Field(None, max_length=128)


class VoidSessionAdvanceRequest(BaseModel):
    reason: Optional[str] = Field(None, max_length=500, description="Motivo opcional de anulación")


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
        tip_amount=body.tip_amount,
        tip_source=body.tip_source,
        tip_taxable=body.tip_taxable,
        served_by_member_id=body.served_by_member_id,
        reason=body.reason,
        waros_to_redeem=body.waros_to_redeem,
        waro_reward_id=body.waro_reward_id,
    )


@router.get("/{table_id}/session-advances", dependencies=[Depends(require_module(Module.POS))])
async def list_session_advances(request: Request, table_id: UUID):
    """List session-scoped minimum-consumption advances for the open table session."""
    return await table_session_advances_service.list_session_advances(request, table_id)


@router.post("/{table_id}/session-advances", dependencies=[Depends(require_module(Module.POS))])
async def create_session_advance(
    request: Request,
    table_id: UUID,
    body: CreateSessionAdvanceRequest,
):
    """Create a session-scoped advance without customer wallet or order settlement."""
    return await table_session_advances_service.create_session_advance(
        request,
        table_id,
        body.amount_cop,
        body.payment_method,
        body.payment_method_id,
        notes=body.notes,
        idempotency_key=body.idempotency_key,
    )


@router.delete("/{table_id}/session-advances/{advance_id}", dependencies=[Depends(require_module(Module.POS))])
async def void_session_advance(
    request: Request,
    table_id: UUID,
    advance_id: UUID,
    body: VoidSessionAdvanceRequest = Body(default_factory=VoidSessionAdvanceRequest),
):
    """Void a session advance and reverse its liability/payment GL when accounts exist."""
    return await table_session_advances_service.void_session_advance(
        request, table_id, advance_id, body.reason,
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
        tip_amount=body.tip_amount,
        tip_source=body.tip_source,
        tip_taxable=body.tip_taxable,
    )


class VoidSessionPaymentRequest(BaseModel):
    reason: Optional[str] = Field(None, description="Issue warocol.com#649 — motivo opcional de la anulación (auditoría).")


@router.delete("/{table_id}/payments/{payment_id}", dependencies=[Depends(require_module(Module.POS))])
async def void_session_payment(
    request: Request,
    table_id: UUID,
    payment_id: UUID,
    body: VoidSessionPaymentRequest,
):
    """
    Issue warocol.com#649 — soft-delete a mesa partial payment (with its
    proportional siblings). Reopens the session if the void cleared the
    closing payment and auto-reverses the GL entries atomically.
    """
    return await tables_service.void_table_payment(
        request, table_id, payment_id, body.reason,
    )


@router.get("/{table_id}/current", dependencies=[Depends(require_module(Module.POS))])
async def get_current_session(request: Request, table_id: UUID):
    """
    Get the open session for a table with all linked orders and running total.
    Returns 404 if no open session exists.
    """
    return await tables_service.get_current_session(request, table_id)


@router.get("/{table_id}/comandas", dependencies=[Depends(require_module(Module.POS))])
async def get_table_session_comandas(request: Request, table_id: UUID):
    """
    Get persisted printable comandas for the table's currently open session.
    Returns 404 if no open session exists.
    """
    return await tables_service.get_table_session_comandas(request, table_id)


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
                {"id": m.id, "name": m.name, "price": m.price, "quantity": m.quantity}
                for m in (item.modifiers or [])
            ],
            "notes": item.notes,
        }
        for item in body.items
    ]
    return await tables_service.add_tab_items(request, table_id, items)


@router.post("/{table_id}/tab/defer-delivery-payment", dependencies=[Depends(require_module(Module.POS))])
async def defer_tab_delivery_payment(
    request: Request,
    table_id: UUID,
    body: TabDeferDeliveryPaymentRequest,
):
    """
    Convert the open bar tab into a delivery order that remains pending until
    the payment method is known.
    """
    return await tables_service.defer_tab_delivery_payment(
        request,
        table_id,
        body.customer_id,
        body.delivery_address_id,
        body.delivery_instructions,
    )


class RemoveTabItemRequest(BaseModel):
    reason: Optional[str] = Field(
        None,
        description="warocol.com#786 — required when the line was fired to kitchen",
    )


@router.delete("/{table_id}/tab/items/{order_item_id}", dependencies=[Depends(require_module(Module.POS))])
async def remove_tab_item(
    request: Request,
    table_id: UUID,
    order_item_id: UUID,
    body: RemoveTabItemRequest = Body(default_factory=RemoveTabItemRequest),
):
    """Remove an order item from the running tab."""
    return await tables_service.remove_tab_item(
        request, table_id, order_item_id, body.reason,
    )


@router.patch("/{table_id}/tab/items/{order_item_id}", dependencies=[Depends(require_module(Module.POS))])
async def update_tab_item_quantity(request: Request, table_id: UUID, order_item_id: UUID, body: UpdateTabItemRequest):
    """Update the quantity of an order item in the running tab."""
    return await tables_service.update_tab_item_quantity(
        request, table_id, order_item_id, body.quantity, body.reason,
    )


@router.get(
    "/{table_id}/tab/items/{order_item_id}/edit-eligibility",
    dependencies=[Depends(require_module(Module.POS))],
)
async def tab_item_edit_eligibility(
    request: Request,
    table_id: UUID,
    order_item_id: UUID,
    record_attempt: bool = Query(False, description="Record bitácora when edit is blocked"),
):
    """Whether tab line modifiers/notes can be edited (kitchen-acceptance gate, #1151)."""
    return await tables_service.get_tab_item_edit_eligibility(
        request, table_id, order_item_id, record_attempt=record_attempt,
    )


@router.patch(
    "/{table_id}/tab/items/{order_item_id}/content",
    dependencies=[Depends(require_module(Module.POS))],
)
async def update_tab_item_content(
    request: Request,
    table_id: UUID,
    order_item_id: UUID,
    body: UpdateTabItemContentRequest,
):
    """Replace modifiers and notes on a tab line (#1151)."""
    modifiers = [
        {"id": m.id, "name": m.name, "price": m.price, "quantity": m.quantity}
        for m in body.modifiers
    ]
    return await tables_service.update_tab_item_content(
        request, table_id, order_item_id, modifiers, body.notes,
    )


class TabPromoOptOutRequest(BaseModel):
    promo_opt_out: bool = Field(
        description="When true, skip automatic promotions for this tab line.",
    )


@router.patch(
    "/{table_id}/tab/items/{order_item_id}/promo-opt-out",
    dependencies=[Depends(require_module(Module.POS))],
)
async def update_tab_item_promo_opt_out(
    request: Request,
    table_id: UUID,
    order_item_id: UUID,
    body: TabPromoOptOutRequest,
):
    """Toggle per-line promotion opt-out for a mesa/tab item (warocol.com#1003)."""
    return await tables_service.update_tab_item_promo_opt_out(
        request, table_id, order_item_id, body.promo_opt_out,
    )


class ClearTabRequest(BaseModel):
    reason: Optional[str] = Field(
        None,
        description="Motivo de vaciar cuenta — required when pending tab lines exist",
    )


@router.delete("/{table_id}/tab", dependencies=[Depends(require_module(Module.POS))])
async def clear_tab(
    request: Request,
    table_id: UUID,
    body: ClearTabRequest = Body(default_factory=ClearTabRequest),
):
    """
    Delete all pending orders for the active session without closing it.
    The table stays open and ready for new orders.
    """
    return await tables_service.clear_tab(request, table_id, body.reason)


@router.post("/{table_id}/bill", dependencies=[Depends(require_module(Module.POS))])
async def request_bill(request: Request, table_id: UUID):
    """
    Mark a table as bill_requested (status: open → bill_requested).
    Returns 409 if table is not open.
    """
    return await tables_service.request_bill(request, table_id)


@router.post("/{table_id}/fire", dependencies=[Depends(require_module(Module.POS))])
async def fire_table_items(
    request: Request,
    table_id: UUID,
    body: Optional[FireTableItemsRequest] = None,
):
    """
    Explicitly fire 'new' items in the table session to the kitchen.
    Optional item_ids: fire only selected order_items (#753).
    Returns comanda summaries and fired item count.
    """
    item_ids = body.item_ids if body else None
    return await tables_service.fire_table_items(request, table_id, item_ids=item_ids)


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

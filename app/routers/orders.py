"""
Orders Router
Endpoints for listing and managing orders
"""
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from datetime import date
from typing import Optional, List
from uuid import UUID
from pydantic import BaseModel, EmailStr, Field
from app.core.dependencies import require_invoicing_ready
from app.core.middleware import require_valid_session
from app.core.permissions import Module, require_module
from app.services import orders_service, facturacion_service, invoice_email_tracking_service

router = APIRouter(prefix="/orders", tags=["Orders"])


@router.get("/dashboard", dependencies=[Depends(require_module(Module.VENTAS))])
async def get_orders_dashboard(
    request: Request,
    payment_method: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    category_id: Optional[str] = Query(None),
):
    """
    Returns all /ventas dashboard metrics in a single DB query:
    - main: all-time totals (filtered by payment_method/status if provided)
    - month: current month-to-date
    - year: current year-to-date
    - commission_savings: savings vs. marketplace apps
    """
    import logging
    logging.getLogger(__name__).info(f"[dashboard] payment_method={payment_method!r} status={status!r}")
    return await orders_service.get_orders_dashboard(
        request,
        payment_method=payment_method,
        status=status,
        category_id=category_id,
    )


@router.get("/metrics", dependencies=[Depends(require_module(Module.VENTAS))])
async def get_orders_metrics(
    request: Request,
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    payment_method: Optional[str] = Query(None),
    payment_method_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    category_id: Optional[str] = Query(None),
):
    return await orders_service.get_orders_metrics(
        request, date_from=date_from, date_to=date_to,
        payment_method=payment_method, payment_method_id=payment_method_id,
        status=status,
        category_id=category_id,
    )


@router.get("/sales-flow", dependencies=[Depends(require_module(Module.VENTAS))])
async def get_sales_flow(
    request: Request,
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    payment_method: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    category_id: Optional[str] = Query(None),
):
    """
    Get sales flow data with intelligent comparison period

    - Ranges ≤30 days: Compare with previous period
    - Ranges >30 days: Compare with same period last year
    - Auto-grouping: hourly (≤3 days), daily (4-90 days), weekly (>90 days)

    Query parameters:
    - date_from: Start date (YYYY-MM-DD)
    - date_to: End date (YYYY-MM-DD)
    - payment_method: Filter by payment method (cash, card, digital)
    - status: Filter by status (completed, cancelled, pending)
    """
    return await orders_service.get_sales_flow(
        request,
        date_from=date_from,
        date_to=date_to,
        payment_method=payment_method,
        status=status,
        category_id=category_id,
    )


@router.post("/export", dependencies=[Depends(require_module(Module.VENTAS))])
async def export_orders(
    request: Request,
    search: Optional[str] = Query(None),
    search_field: Optional[str] = Query(None),
    payment_method: Optional[str] = Query(None),
    payment_method_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    sort_field: str = Query("order_date"),
    sort_direction: str = Query("desc"),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    # warocol.com#640 — tips-only export mode
    tips_only: bool = Query(False, description="When true, restrict to orders with tip_amount > 0 and emit tip-specific CSV columns."),
    member_id: Optional[str] = Query(None, description="Filter by served_by_member_id (tips-only flow)."),
    channel: Optional[str] = Query(None, description="Filter by channel: 'pos' | 'mesa' | 'online' (tips-only flow)."),
):
    return await orders_service.export_orders_to_email(
        request,
        search=search,
        search_field=search_field,
        payment_method=payment_method,
        payment_method_id=payment_method_id,
        status=status,
        sort_field=sort_field,
        sort_direction=sort_direction,
        date_from=date_from,
        date_to=date_to,
        tips_only=tips_only,
        member_id=member_id,
        channel=channel,
    )


@router.get("", dependencies=[Depends(require_module(Module.VENTAS))])
async def get_orders(
    request: Request,
    limit: int = Query(50, ge=1, le=250),
    offset: int = Query(0, ge=0),
    search: Optional[str] = Query(None),
    search_field: Optional[str] = Query(None),
    payment_method: Optional[str] = Query(None),
    payment_method_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    sort_field: str = Query("order_date"),
    sort_direction: str = Query("desc"),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    delivery_only: Optional[bool] = Query(None, description="When true, narrow results to orders with delivery_address_id IS NOT NULL"),
):
    return await orders_service.get_orders_list(
        request,
        limit=limit,
        offset=offset,
        search=search,
        search_field=search_field,
        payment_method=payment_method,
        payment_method_id=payment_method_id,
        status=status,
        sort_field=sort_field,
        sort_direction=sort_direction,
        date_from=date_from,
        date_to=date_to,
        delivery_only=delivery_only,
    )


@router.get("/customers", dependencies=[Depends(require_module(Module.VENTAS))])
async def get_customers(
    request: Request,
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    payment_method: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0)
):
    """
    Get list of customers aggregated from POS orders, ranked by total spent.

    Query parameters:
    - date_from: Start date (YYYY-MM-DD)
    - date_to: End date (YYYY-MM-DD)
    - payment_method: Filter by payment method
    - status: Filter by order status
    - search: Partial case-insensitive match on customer name OR phone number
    - limit: Number of customers to return (1-500, default 100)
    - offset: Number of customers to skip (default 0)
    """
    return await orders_service.get_customers_list(
        request,
        date_from=date_from,
        date_to=date_to,
        payment_method=payment_method,
        status=status,
        search=search,
        limit=limit,
        offset=offset
    )


@router.get("/customers/{customer_id}", dependencies=[Depends(require_module(Module.VENTAS))])
async def get_customer_detail(
    request: Request,
    customer_id: UUID,
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100)
):
    """
    Get a single customer's aggregate stats and paginated POS order history.

    Query parameters:
    - date_from: Filter order history start date (YYYY-MM-DD)
    - date_to: Filter order history end date (YYYY-MM-DD)
    - page: Page number (default 1)
    - per_page: Orders per page (1-100, default 20)
    """
    return await orders_service.get_customer_detail(
        request,
        customer_id=customer_id,
        date_from=date_from,
        date_to=date_to,
        page=page,
        per_page=per_page
    )


class ManualOrderModifier(BaseModel):
    id: str
    name: str
    price: float = 0.0
    quantity: int = Field(default=1, ge=1)


class ManualOrderItem(BaseModel):
    product_id: str
    quantity: float = Field(gt=0)
    unit_price: float = Field(ge=0)
    modifiers: List[ManualOrderModifier] = []


class ManualOrderPayment(BaseModel):
    amount: float = Field(gt=0)
    payment_method: str
    payment_method_id: Optional[str] = None
    cash_received: Optional[float] = None


class CreateManualOrderRequest(BaseModel):
    order_date: str
    payment_method: str
    # Issue #533 follow-up — when the operator picks a specific method from the
    # dropdown (Nequi, Daviplata, etc.), persist the method UUID so the order
    # is linked to the right gl_account_code-bearing method (and not just the
    # group slug). Optional for backward compatibility.
    payment_method_id: Optional[str] = None
    customer_id: Optional[str] = None
    discount_type: Optional[str] = None
    discount_value: Optional[float] = None
    payments: Optional[List[ManualOrderPayment]] = None
    items: List[ManualOrderItem] = Field(min_length=1)
    wompi_collection: bool = False


@router.post("/manual", dependencies=[Depends(require_module(Module.VENTAS))])
async def create_manual_order(
    request: Request,
    data: CreateManualOrderRequest
):
    """
    Create a manual order with a custom date, bypassing the POS cart.
    Useful for registering sales that occurred outside the POS system.
    """
    return await orders_service.create_manual_order(
        request,
        order_date=data.order_date,
        payment_method=data.payment_method,
        payment_method_id=data.payment_method_id,
        items=[item.model_dump() for item in data.items],
        customer_id=data.customer_id,
        discount_type=data.discount_type,
        discount_value=data.discount_value,
        payments=[payment.model_dump() for payment in data.payments] if data.payments else None,
        wompi_collection=data.wompi_collection,
    )


@router.get("/products-sold", dependencies=[Depends(require_module(Module.VENTAS))])
async def get_products_sold(
    request: Request,
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    category_id: Optional[str] = Query(None),
    sort: Optional[str] = Query("qty_desc"),
    search: Optional[str] = Query(None, description="Filter by product name (partial match)"),
    channel: Optional[str] = Query(None, description="Filter by channel: 'pos' | 'mesa' | 'online'"),
    limit: int = Query(25, ge=1, le=250),
    offset: int = Query(0, ge=0),
):
    """
    Get products sold report aggregated by product.

    Query parameters:
    - date_from: Start date (YYYY-MM-DD)
    - date_to: End date (YYYY-MM-DD)
    - category_id: Filter by category UUID
    - sort: qty_desc | revenue_desc | name_asc
    - search: Product name (ILIKE)
    - channel: pos | mesa | online
    - limit / offset: page product rows (totals stay full-filter)
    """
    return await orders_service.get_products_sold(
        request,
        date_from=date_from,
        date_to=date_to,
        category_id=category_id,
        sort=sort or "qty_desc",
        search=search,
        channel=channel,
        limit=limit,
        offset=offset,
    )


@router.get("/tips", dependencies=[Depends(require_module(Module.VENTAS))])
async def get_tips(
    request: Request,
    limit: int = Query(50, ge=1, le=250),
    offset: int = Query(0, ge=0),
    search: Optional[str] = Query(None, description="Free-text search on order_number"),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    member_id: Optional[UUID] = Query(None, description="Filter by served_by_member_id (single waiter)"),
    payment_method: Optional[str] = Query(None),
    channel: Optional[str] = Query(None, description="'pos' | 'mesa' | 'online'"),
    sort_field: str = Query("order_date"),
    sort_direction: str = Query("desc"),
):
    """
    History of orders with captured tips (warocol.com#637). Powers /ventas/propinas.

    Always scoped to the current tenant and to rows where tip_amount > 0.
    Response includes pagination + aggregates (sum_tip, count_with_tip, avg_pct)
    computed against the same WHERE so totals match the visible page.
    """
    return await orders_service.get_tips_list(
        request,
        limit=limit,
        offset=offset,
        search=search,
        date_from=date_from,
        date_to=date_to,
        member_id=member_id,
        payment_method=payment_method,
        channel=channel,
        sort_field=sort_field,
        sort_direction=sort_direction,
    )


@router.get("/{order_id}", dependencies=[Depends(require_module(Module.VENTAS))])
async def get_order(
    request: Request,
    order_id: UUID
):
    """
    Get single order by ID
    """
    return await orders_service.get_order_by_id(request, order_id)


class UpdateOrderStatusRequest(BaseModel):
    status: str = Field(..., description="completed | cancelled | pending")
    payment_method: Optional[str] = Field(None, description="Payment method group slug")
    payment_method_id: Optional[str] = Field(None, description="UUID of the selected payment_methods row")
    customer_id: Optional[str] = Field(None, description="UUID of customer to associate when required by payment method")
    reason: Optional[str] = Field(None, description="Required when cancelling")
    cash_received: Optional[float] = Field(None, ge=0, description="Cash handed over when completing with cash")
    credit_due_date: Optional[date] = Field(None, description="Due date when completing with credit")
    served_by_member_id: Optional[UUID] = Field(None, description="Waiter member UUID at complete")


class AssociateOrderCustomerRequest(BaseModel):
    customer_id: UUID = Field(..., description="UUID of customer to associate before electronic invoicing")


class BulkUpdateStatusRequest(BaseModel):
    order_ids: List[str] = Field(..., min_length=1)
    status: str = Field(..., description="completed | cancelled | pending")
    payment_method: Optional[str] = Field(None, description="Payment method group slug")
    payment_method_id: Optional[str] = Field(None, description="UUID of the selected payment_methods row")
    customer_id: Optional[str] = Field(None, description="UUID of customer to associate")


@router.patch("/bulk-status", dependencies=[Depends(require_module(Module.VENTAS))])
async def bulk_update_order_status(
    request: Request,
    body: BulkUpdateStatusRequest
):
    """Bulk update status for multiple orders."""
    return await orders_service.bulk_update_order_status(
        request,
        body.order_ids,
        body.status,
        body.payment_method,
        body.customer_id,
        body.payment_method_id,
    )


@router.patch("/{order_id}/status", dependencies=[Depends(require_module(Module.VENTAS))])
async def update_order_status(
    request: Request,
    order_id: UUID,
    body: UpdateOrderStatusRequest
):
    """
    Update the status of a mesa order. Also accepts payment_method when completing.
    """
    return await orders_service.update_order_status(
        request,
        order_id,
        body.status,
        body.payment_method,
        body.payment_method_id,
        body.customer_id,
        body.reason,
        cash_received=body.cash_received,
        credit_due_date=body.credit_due_date,
        served_by_member_id=body.served_by_member_id,
    )


@router.patch("/{order_id}/customer", dependencies=[Depends(require_module(Module.VENTAS))])
async def associate_order_customer(
    request: Request,
    order_id: UUID,
    body: AssociateOrderCustomerRequest,
):
    """
    Associate or change the customer before an electronic invoice exists.
    """
    return await orders_service.associate_order_customer(
        request,
        order_id,
        body.customer_id,
    )


@router.get("/{order_id}/items", dependencies=[Depends(require_module(Module.VENTAS))])
async def get_order_items(
    request: Request,
    order_id: UUID
):
    """
    Get order items with modifiers
    """
    return await orders_service.get_order_items(request, order_id)


@router.delete("/{order_id}/items/{item_id}", dependencies=[Depends(require_module(Module.VENTAS))])
async def delete_order_item(
    request: Request,
    order_id: UUID,
    item_id: UUID
):
    """
    Delete an order item and its associated modifiers.
    Also updates the order total.
    """
    return await orders_service.delete_order_item(request, order_id, item_id)


@router.delete("/{order_id}/items/{item_id}/modifiers/{modifier_id}", dependencies=[Depends(require_module(Module.VENTAS))])
async def delete_order_item_modifier(
    request: Request,
    order_id: UUID,
    item_id: UUID,
    modifier_id: UUID
):
    """
    Delete a modifier from an order item.
    Also updates the item subtotal and order total.
    """
    return await orders_service.delete_order_item_modifier(request, order_id, item_id, modifier_id)


# ── Electronic invoicing (issue #128) ─────────────────────────────────────────

@router.post("/{order_id}/invoice", tags=["Invoices"], dependencies=[Depends(require_module(Module.VENTAS))])
async def emit_order_invoice(
    request: Request,
    order_id: UUID,
    _readiness: dict = Depends(require_invoicing_ready),
):
    """
    Emit a DIAN electronic invoice for a completed POS order.

    Delegates to api-facturacion microservice via internal Docker network.
    Idempotent: calling twice for the same order returns the existing invoice.

    Returns 403 if the tenant is not ready for electronic invoicing
    (issue #130: missing dev flag, fiscal data, or active resolution).

    Returns invoice identifiers, status, PDF URL when available, and optional
    fiscal presentation fields for receipt/email rendering.
    """
    session_context = require_valid_session(request)
    return await facturacion_service.emit_invoice(
        order_id=str(order_id),
        tenant_id=str(session_context.tenant_id),
        order_type='pos',
    )


@router.get("/{order_id}/invoice", tags=["Invoices"], dependencies=[Depends(require_module(Module.VENTAS))])
async def get_order_invoice(
    request: Request,
    order_id: UUID,
):
    """
    Get the current electronic invoice for an order.

    Returns invoice status, CUFE, a fresh presigned PDF URL (1h TTL), safe
    attachment flags, and optional fiscal presentation fields.
    Returns 404 if no invoice has been emitted for this order yet.
    """
    session_context = require_valid_session(request)
    result = await facturacion_service.get_order_invoice(
        order_id=str(order_id),
        tenant_id=str(session_context.tenant_id),
    )
    # Return 200 + null when no FE yet so browsers don't log a console 404 on
    # every sales detail open (still "no invoice"; front treats null the same).
    if result is None:
        return None
    return result


@router.get("/{order_id}/invoice/dian-status", tags=["Invoices"], dependencies=[Depends(require_module(Module.VENTAS))])
async def get_order_invoice_dian_status(
    request: Request,
    order_id: UUID,
):
    """
    Return the DIAN verification status for an order's invoice.

    Reads electronic_invoices directly from DB — no api-facturacion call needed.
    Returns 404 if no invoice has been emitted for this order yet.
    """
    session_context = require_valid_session(request)
    result = await facturacion_service.get_dian_status(
        order_id=str(order_id),
        tenant_id=str(session_context.tenant_id),
    )
    if result is None:
        raise HTTPException(status_code=404, detail="No invoice found for this order")
    return result


class SendInvoiceEmailBody(BaseModel):
    email: EmailStr = Field(..., description="Recipient email address")


@router.post(
    "/{order_id}/invoice/send-email",
    tags=["Invoices"],
    dependencies=[Depends(require_module(Module.VENTAS))],
)
async def send_order_invoice_email(
    request: Request,
    order_id: UUID,
    body: SendInvoiceEmailBody,
):
    """Send the fiscal receipt email for this order's accepted invoice."""
    return await orders_service.send_invoice_email(request, order_id, body.email)


@router.get(
    "/{order_id}/invoice/email-history",
    tags=["Invoices"],
    dependencies=[Depends(require_module(Module.VENTAS))],
)
async def get_order_invoice_email_history(
    request: Request,
    order_id: UUID,
):
    """Delivery history of invoice emails for this order (api-warolabs#657).

    Newest first. `sent` means SES accepted the request — not proof of
    delivery or human read. `open_count` is only an open-detection signal
    from the tracking pixel.
    """
    session_context = require_valid_session(request)
    deliveries = await invoice_email_tracking_service.list_deliveries_for_order(
        tenant_id=session_context.tenant_id,
        order_id=order_id,
    )
    return {"deliveries": deliveries}

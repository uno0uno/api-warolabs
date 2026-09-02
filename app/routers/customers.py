"""
Customers Router - HTTP endpoints for customer management
"""
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Request, Query
from pydantic import BaseModel, Field, EmailStr
from uuid import UUID
from app.core.permissions import Module, require_module
from app.core.middleware import require_valid_session
from app.services.customers_service import (
    search_or_create_customer,
    search_customer_by_phone,
    search_customers_by_query,
    get_customer_insights,
    get_customer_by_id,
    update_customer,
)
from app.services.customer_wallet_service import (
    get_customer_wallet,
    recharge_customer_wallet,
    refund_customer_wallet,
)
from app.services.email_helpers import send_wallet_recharge_receipt_email
from app.models.customer import (
    CustomerSearchOrCreate,
    CustomerResponse,
    CustomerSearchResponse,
    CustomerQuerySearchResponse,
    CustomerInsightsResponse,
    CustomerUpdate,
    CustomerUpdateResponse,
)

router = APIRouter()


class WalletRechargeRequest(BaseModel):
    amount_cop: Decimal = Field(..., gt=0, description="Recarga en COP")
    payment_method: str = Field(..., description="cash | card | digital")
    payment_method_id: Optional[UUID] = None
    notes: Optional[str] = None
    idempotency_key: Optional[str] = Field(None, max_length=128)


class WalletRefundRequest(BaseModel):
    amount_cop: Decimal = Field(..., gt=0, description="Devolución en COP")
    payment_method: str = Field(..., description="cash | card | digital")
    payment_method_id: Optional[UUID] = None
    notes: Optional[str] = None


class SendWalletRechargeReceiptRequest(BaseModel):
    email: EmailStr = Field(..., description="Recipient email")
    customer_name: str = Field(..., min_length=1)
    recharge_date: str = Field(..., description="ISO datetime or formatted date from client")
    payment_method_label: str = Field(..., min_length=1)
    amount_cop: float = Field(..., gt=0)
    balance_after_cop: float = Field(..., ge=0)
    notes: Optional[str] = None
    business_name: Optional[str] = None
    business_address: Optional[str] = None
    business_city: Optional[str] = None
    business_phone: Optional[str] = None


@router.get(
    "/{customer_id}/wallet",
    status_code=200,
    dependencies=[Depends(require_module(Module.VENTAS))],
)
async def get_customer_wallet_endpoint(
    request: Request,
    customer_id: UUID,
    limit: int = Query(20, ge=1, le=50),
):
    """Saldo COP y movimientos recientes de la billetera del cliente."""
    return await get_customer_wallet(request, customer_id, limit=limit)


@router.post(
    "/{customer_id}/wallet/recharge",
    status_code=200,
    dependencies=[Depends(require_module(Module.VENTAS))],
)
async def recharge_customer_wallet_endpoint(
    request: Request,
    customer_id: UUID,
    body: WalletRechargeRequest,
):
    """Recarga de anticipo (staff). Registra Dr caja/banco / Cr 2810."""
    return await recharge_customer_wallet(
        request,
        customer_id,
        body.amount_cop,
        body.payment_method,
        body.payment_method_id,
        body.notes,
        body.idempotency_key,
    )


@router.post(
    "/{customer_id}/wallet/receipt-email",
    status_code=200,
    dependencies=[Depends(require_module(Module.VENTAS))],
)
async def send_wallet_recharge_receipt_email_endpoint(
    request: Request,
    customer_id: UUID,
    body: SendWalletRechargeReceiptRequest,
):
    """Email a wallet recharge receipt after CRM staff recharge."""
    session = require_valid_session(request)
    tenant_id = session.tenant_id if session else None

    success = await send_wallet_recharge_receipt_email(
        customer_email=str(body.email),
        customer_name=body.customer_name,
        recharge_date_label=body.recharge_date,
        payment_method_label=body.payment_method_label,
        amount_cop=body.amount_cop,
        balance_after_cop=body.balance_after_cop,
        notes=body.notes,
        tenant_id=str(tenant_id) if tenant_id else None,
        business_name=body.business_name,
        business_address=body.business_address,
        business_city=body.business_city,
        business_phone=body.business_phone,
    )
    return {"success": success}


@router.post(
    "/{customer_id}/wallet/refund",
    status_code=200,
    dependencies=[Depends(require_module(Module.FINANZAS))],
)
async def refund_customer_wallet_endpoint(
    request: Request,
    customer_id: UUID,
    body: WalletRefundRequest,
):
    """Devolución de saldo a favor (Finanzas)."""
    return await refund_customer_wallet(
        request,
        customer_id,
        body.amount_cop,
        body.payment_method,
        body.payment_method_id,
        body.notes,
    )


@router.post("/search-or-create", response_model=CustomerResponse, status_code=200, dependencies=[Depends(require_module(Module.VENTAS))])
async def search_or_create_customer_endpoint(
    request: Request,
    customer_data: CustomerSearchOrCreate
):
    """
    Search for a customer by phone number, create if doesn't exist.

    **Request Body:**
    - phone_number: Phone number (required)
    - name: Customer name (optional)
    - email: Customer email (optional)

    **Example:**
    ```json
    {
        "phone_number": "3001234567",
        "name": "Juan Pérez",
        "email": "juan@example.com"
    }
    ```

    **Response:**
    - Returns customer data
    - `is_new`: true if customer was just created, false if existing
    """
    return await search_or_create_customer(request, customer_data)


@router.get("/search", response_model=CustomerSearchResponse, status_code=200, dependencies=[Depends(require_module(Module.VENTAS))])
async def search_customer_endpoint(
    request: Request,
    phone_number: str = Query(..., min_length=7, max_length=20, description="Phone number to search")
):
    """
    Search for a customer by phone number.

    **Query Parameters:**
    - phone_number: Phone number to search

    **Example:**
    ```
    GET /customers/search?phone_number=3001234567
    ```

    **Response:**
    - found: true if customer exists, false otherwise
    - customer: Customer data if found, null otherwise
    """
    return await search_customer_by_phone(request, phone_number)


@router.get("/search-by-query", response_model=CustomerQuerySearchResponse, status_code=200, dependencies=[Depends(require_module(Module.VENTAS))])
async def search_customers_by_query_endpoint(
    request: Request,
    q: str = Query(..., min_length=1, max_length=100, description="Partial name or phone to search"),
    limit: int = Query(20, ge=1, le=50, description="Maximum results to return")
):
    """
    Search customers by partial name OR phone number (case-insensitive).
    Results are scoped to the current tenant.

    **Query Parameters:**
    - q: Partial name or phone number
    - limit: Max results (default 20, max 50)

    **Example:**
    ```
    GET /customers/search-by-query?q=ander&limit=20
    ```
    """
    return await search_customers_by_query(request, q, limit)


@router.patch("/{customer_id}", response_model=CustomerUpdateResponse, status_code=200, dependencies=[Depends(require_module(Module.VENTAS))])
async def update_customer_endpoint(
    request: Request,
    customer_id: UUID,
    update_data: CustomerUpdate,
):
    """
    Update name, email, and/or phone_number of a customer.
    Only provided fields are updated. Customer must belong to the current tenant.
    """
    return await update_customer(request, customer_id, update_data)


@router.get("/{customer_id}", response_model=CustomerUpdateResponse, status_code=200, dependencies=[Depends(require_module(Module.VENTAS))])
async def get_customer_by_id_endpoint(
    request: Request,
    customer_id: UUID,
):
    """
    Get a single customer by id, including fiscal fields.
    Customer must belong to the current tenant.
    """
    return await get_customer_by_id(request, customer_id)


@router.get("/{customer_id}/insights", response_model=CustomerInsightsResponse, status_code=200, dependencies=[Depends(require_module(Module.VENTAS))])
async def get_customer_insights_endpoint(
    request: Request,
    customer_id: UUID
):
    """
    Return aggregated purchase stats for a customer scoped to the current tenant.

    **Path Parameters:**
    - customer_id: Customer UUID

    **Response:**
    - orders_count: Total completed orders
    - last_order_date: Most recent order timestamp
    - avg_ticket: Average order value (COP, integer)
    - top_product_name: Most purchased product name
    - top_product_count: Units of top product purchased
    - avg_days_between_visits: Average days between orders (null if < 2 orders)

    All metric fields are null when orders_count == 0.
    """
    return await get_customer_insights(request, customer_id)

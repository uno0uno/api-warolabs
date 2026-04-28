"""
Customers Router - HTTP endpoints for customer management
"""
from fastapi import APIRouter, Request, Query
from uuid import UUID
from app.services.customers_service import (
    search_or_create_customer,
    search_customer_by_phone,
    search_customers_by_query,
    get_customer_insights,
    get_customer_by_id,
    update_customer,
)
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


@router.post("/search-or-create", response_model=CustomerResponse, status_code=200)
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


@router.get("/search", response_model=CustomerSearchResponse, status_code=200)
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


@router.get("/search-by-query", response_model=CustomerQuerySearchResponse, status_code=200)
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


@router.patch("/{customer_id}", response_model=CustomerUpdateResponse, status_code=200)
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


@router.get("/{customer_id}", response_model=CustomerUpdateResponse, status_code=200)
async def get_customer_by_id_endpoint(
    request: Request,
    customer_id: UUID,
):
    """
    Get a single customer by id, including fiscal fields.
    Customer must belong to the current tenant (tenant_members).
    """
    return await get_customer_by_id(request, customer_id)


@router.get("/{customer_id}/insights", response_model=CustomerInsightsResponse, status_code=200)
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

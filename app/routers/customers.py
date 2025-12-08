"""
Customers Router - HTTP endpoints for customer management
"""
from fastapi import APIRouter, Request, Query
from app.services.customers_service import (
    search_or_create_customer,
    search_customer_by_phone
)
from app.models.customer import (
    CustomerSearchOrCreate,
    CustomerResponse,
    CustomerSearchResponse
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

from fastapi import APIRouter, Request, Response, Query
from uuid import UUID
from typing import Optional
from app.services.purchases_service import (
    get_purchases_list,
    get_purchase_by_id,
    create_purchase,
    update_purchase,
    delete_purchase
)
from app.models.purchase import (
    Purchase,
    PurchaseCreate,
    PurchaseUpdate,
    PurchaseResponse,
    PurchasesListResponse
)

router = APIRouter()

@router.get("/next-number")
async def get_next_purchase_number(
    request: Request,
    response: Response
):
    """
    Get the next auto-generated purchase number
    Preview only - actual number is generated on creation
    """
    from app.core.middleware import require_valid_session
    from app.database import get_db_connection
    from datetime import datetime

    session_context = require_valid_session(request)
    tenant_id = session_context.tenant_id

    if not tenant_id:
        return {"next_number": "WR-2025-0001"}

    async with get_db_connection() as conn:
        current_year = datetime.now().year

        last_purchase = await conn.fetchrow("""
            SELECT purchase_number
            FROM tenant_purchases
            WHERE tenant_id = $1
                AND purchase_number LIKE $2
            ORDER BY created_at DESC
            LIMIT 1
        """, tenant_id, f'WR-{current_year}-%')

        if last_purchase and last_purchase['purchase_number']:
            last_number = int(last_purchase['purchase_number'].split('-')[-1])
            next_number = last_number + 1
        else:
            next_number = 1

        return {
            "next_number": f"WR-{current_year}-{next_number:04d}"
        }

@router.get("", response_model=PurchasesListResponse)
async def get_purchases_endpoint(
    request: Request,
    response: Response,
    page: int = Query(default=1, ge=1, description="Page number"),
    limit: int = Query(default=50, ge=1, le=250, description="Items per page"),
    search: Optional[str] = Query(default=None, description="Search by purchase number or invoice number"),
    status: Optional[str] = Query(default=None, description="Filter by status"),
    supplier_id: Optional[UUID] = Query(default=None, description="Filter by supplier ID")
):
    """
    Get purchases list with tenant isolation
    Requires valid session with tenant context
    """
    return await get_purchases_list(
        request, response, page, limit, search, status, supplier_id
    )

@router.get("/{purchase_id}", response_model=PurchaseResponse)
async def get_purchase_endpoint(
    purchase_id: UUID,
    request: Request,
    response: Response
):
    """
    Get a specific purchase by ID with tenant isolation
    """
    return await get_purchase_by_id(request, response, purchase_id)

@router.post("", response_model=PurchaseResponse)
async def create_purchase_endpoint(
    purchase_data: PurchaseCreate,
    request: Request,
    response: Response
):
    """
    Create a new purchase with tenant isolation
    """
    return await create_purchase(request, response, purchase_data)

@router.put("/{purchase_id}", response_model=PurchaseResponse)
async def update_purchase_endpoint(
    purchase_id: UUID,
    purchase_data: PurchaseUpdate,
    request: Request,
    response: Response
):
    """
    Update an existing purchase with tenant isolation
    """
    return await update_purchase(request, response, purchase_id, purchase_data)

@router.delete("/{purchase_id}")
async def delete_purchase_endpoint(
    purchase_id: UUID,
    request: Request,
    response: Response
):
    """
    Delete a purchase with tenant isolation
    """
    return await delete_purchase(request, response, purchase_id)

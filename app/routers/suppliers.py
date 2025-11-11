from fastapi import APIRouter, Request, Response, HTTPException, Query
from uuid import UUID
from typing import Optional
from app.services.suppliers_service import (
    get_suppliers_list,
    get_supplier_by_id,
    create_supplier,
    update_supplier,
    delete_supplier
)
from app.models.supplier import (
    Supplier,
    SupplierCreate,
    SupplierUpdate,
    SupplierResponse,
    SuppliersListResponse
)

router = APIRouter()

@router.get("", response_model=SuppliersListResponse)
async def get_suppliers_endpoint(
    request: Request,
    response: Response,
    page: int = Query(default=1, ge=1, description="Page number"),
    limit: int = Query(default=50, ge=1, le=250, description="Items per page"),
    search: Optional[str] = Query(default=None, description="Search by name or tax_id"),
    is_active: Optional[bool] = Query(default=None, description="Filter by active status"),
    payment_terms: Optional[str] = Query(default=None, description="Filter by payment terms")
):
    """
    Get suppliers list with tenant isolation
    Requires valid session with tenant context
    """
    return await get_suppliers_list(
        request, response, page, limit, search, is_active, payment_terms
    )

@router.get("/{supplier_id}", response_model=SupplierResponse)
async def get_supplier_endpoint(
    supplier_id: UUID,
    request: Request,
    response: Response
):
    """
    Get a specific supplier by ID with tenant isolation
    """
    return await get_supplier_by_id(request, response, supplier_id)

@router.post("", response_model=SupplierResponse)
async def create_supplier_endpoint(
    supplier_data: SupplierCreate,
    request: Request,
    response: Response
):
    """
    Create a new supplier with tenant isolation
    """
    return await create_supplier(request, response, supplier_data)

@router.put("/{supplier_id}", response_model=SupplierResponse)
async def update_supplier_endpoint(
    supplier_id: UUID,
    supplier_data: SupplierUpdate,
    request: Request,
    response: Response
):
    """
    Update an existing supplier with tenant isolation
    """
    return await update_supplier(request, response, supplier_id, supplier_data)

@router.delete("/{supplier_id}")
async def delete_supplier_endpoint(
    supplier_id: UUID,
    request: Request,
    response: Response
):
    """
    Delete a supplier with tenant isolation
    """
    return await delete_supplier(request, response, supplier_id)
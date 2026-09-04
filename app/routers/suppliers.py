from fastapi import APIRouter, Body, Depends, Request, Response, Query
from fastapi import HTTPException
from uuid import UUID
from typing import Optional
from app.core.permissions import Module, require_module
from app.services.suppliers_service import (
    get_suppliers_list,
    get_supplier_by_id,
    create_supplier,
    update_supplier,
    delete_supplier
)
from app.services.payment_agreements_service import (
    get_payment_agreements_list,
    get_payment_agreement_by_id,
    create_payment_agreement,
    update_payment_agreement,
    delete_payment_agreement
)
from app.models.supplier import (
    SupplierCreate,
    SupplierUpdate,
    SupplierResponse,
    SuppliersListResponse
)
from app.models.payment_agreement import (
    PaymentAgreementCreate,
    PaymentAgreementUpdate,
    PaymentAgreementResponse,
    PaymentAgreementsListResponse
)

router = APIRouter()

@router.get("", response_model=SuppliersListResponse, dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def get_suppliers_endpoint(
    request: Request,
    response: Response,
    page: int = Query(default=1, ge=1, description="Page number"),
    limit: int = Query(default=50, ge=1, le=250, description="Items per page"),
    search: Optional[str] = Query(default=None, description="Search term"),
    search_field: Optional[str] = Query(default=None, description="Field to search in (name, tax_id, email, phone)"),
    is_active: Optional[bool] = Query(default=None, description="Filter by active status"),
    payment_terms: Optional[str] = Query(default=None, description="Filter by payment terms")
):
    """
    Get suppliers list with tenant isolation
    Requires valid session with tenant context
    """
    return await get_suppliers_list(
        request, response, page, limit, search, search_field, is_active, payment_terms
    )

@router.get("/{supplier_id}", response_model=SupplierResponse, dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def get_supplier_endpoint(
    supplier_id: UUID,
    request: Request,
    response: Response
):
    """
    Get a specific supplier by ID with tenant isolation
    """
    return await get_supplier_by_id(request, response, supplier_id)

@router.post("", response_model=SupplierResponse, dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def create_supplier_endpoint(
    supplier_data: SupplierCreate,
    request: Request,
    response: Response
):
    """
    Create a new supplier with tenant isolation
    """
    return await create_supplier(request, response, supplier_data)

@router.put("/{supplier_id}", response_model=SupplierResponse, dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
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

@router.delete("/{supplier_id}", dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def delete_supplier_endpoint(
    supplier_id: UUID,
    request: Request,
    response: Response,
    payload: dict = Body(default={}),
):
    """
    Delete a supplier with tenant isolation. Requires `reason` in body (Bitácora audit).
    """
    reason = (payload or {}).get("reason", "").strip() if isinstance(payload, dict) else ""
    if not reason:
        raise HTTPException(status_code=422, detail="reason is required")
    return await delete_supplier(request, response, supplier_id, reason=reason)

# Payment Agreements Endpoints

@router.get("/{supplier_id}/payment-agreements", response_model=PaymentAgreementsListResponse, dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def get_payment_agreements_endpoint(
    supplier_id: UUID,
    request: Request,
    response: Response
):
    """
    Get all payment agreements for a supplier with tenant isolation
    """
    return await get_payment_agreements_list(request, response, supplier_id)

@router.get("/{supplier_id}/payment-agreements/{agreement_id}", response_model=PaymentAgreementResponse, dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def get_payment_agreement_endpoint(
    supplier_id: UUID,
    agreement_id: UUID,
    request: Request,
    response: Response
):
    """
    Get a specific payment agreement by ID with tenant isolation
    """
    return await get_payment_agreement_by_id(request, response, supplier_id, agreement_id)

@router.post("/{supplier_id}/payment-agreements", response_model=PaymentAgreementResponse, dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def create_payment_agreement_endpoint(
    supplier_id: UUID,
    agreement_data: PaymentAgreementCreate,
    request: Request,
    response: Response
):
    """
    Create a new payment agreement for a supplier with tenant isolation
    """
    return await create_payment_agreement(request, response, supplier_id, agreement_data)

@router.put("/{supplier_id}/payment-agreements/{agreement_id}", response_model=PaymentAgreementResponse, dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def update_payment_agreement_endpoint(
    supplier_id: UUID,
    agreement_id: UUID,
    agreement_data: PaymentAgreementUpdate,
    request: Request,
    response: Response
):
    """
    Update an existing payment agreement with tenant isolation
    """
    return await update_payment_agreement(request, response, supplier_id, agreement_id, agreement_data)

@router.delete("/{supplier_id}/payment-agreements/{agreement_id}", dependencies=[Depends(require_module(Module.ABASTECIMIENTO))])
async def delete_payment_agreement_endpoint(
    supplier_id: UUID,
    agreement_id: UUID,
    request: Request,
    response: Response,
    payload: dict = Body(default={}),
):
    """
    Delete a payment agreement with tenant isolation. Requires `reason` in body (Bitácora audit).
    """
    reason = (payload or {}).get("reason", "").strip() if isinstance(payload, dict) else ""
    if not reason:
        raise HTTPException(status_code=422, detail="reason is required")
    return await delete_payment_agreement(request, response, supplier_id, agreement_id, reason=reason)
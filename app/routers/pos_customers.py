"""POS-scoped customer endpoints for checkout (cashier RBAC).

Mirrors the minimal VENTAS customer subset used by POS checkout without
granting cashiers Module.VENTAS.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.core.permissions import Module, require_module
from app.models.customer import (
    CustomerInsightsResponse,
    CustomerQuerySearchResponse,
    CustomerResponse,
    CustomerSearchOrCreate,
    CustomerUpdate,
    CustomerUpdateResponse,
)
from app.services.customers_service import (
    get_customer_by_id,
    get_customer_insights,
    search_customers_by_query,
    search_or_create_customer,
    update_customer,
)

router = APIRouter(prefix="/pos/customers", tags=["pos"])


@router.post(
    "/search-or-create",
    response_model=CustomerResponse,
    status_code=200,
    dependencies=[Depends(require_module(Module.POS))],
)
async def pos_search_or_create_customer_endpoint(
    request: Request,
    customer_data: CustomerSearchOrCreate,
):
    return await search_or_create_customer(request, customer_data)


@router.get(
    "/search-by-query",
    response_model=CustomerQuerySearchResponse,
    status_code=200,
    dependencies=[Depends(require_module(Module.POS))],
)
async def pos_search_customers_by_query_endpoint(
    request: Request,
    q: str = Query(..., min_length=1, max_length=100),
    limit: int = Query(20, ge=1, le=50),
):
    return await search_customers_by_query(request, q, limit)


@router.get(
    "/{customer_id}",
    response_model=CustomerUpdateResponse,
    status_code=200,
    dependencies=[Depends(require_module(Module.POS))],
)
async def pos_get_customer_by_id_endpoint(
    request: Request,
    customer_id: UUID,
):
    return await get_customer_by_id(request, customer_id)


@router.patch(
    "/{customer_id}",
    response_model=CustomerUpdateResponse,
    status_code=200,
    dependencies=[Depends(require_module(Module.POS))],
)
async def pos_update_customer_endpoint(
    request: Request,
    customer_id: UUID,
    update_data: CustomerUpdate,
):
    return await update_customer(request, customer_id, update_data)


@router.get(
    "/{customer_id}/insights",
    response_model=CustomerInsightsResponse,
    status_code=200,
    dependencies=[Depends(require_module(Module.POS))],
)
async def pos_get_customer_insights_endpoint(
    request: Request,
    customer_id: UUID,
):
    return await get_customer_insights(request, customer_id)

"""
Public API Router
Endpoints for external integrations authenticated via API tokens
"""
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from typing import Optional
from uuid import UUID
from app.services import public_api_service

router = APIRouter(prefix="/v1", tags=["Public API"])


class SalesQueryRequest(BaseModel):
    """Request body para consultar ventas"""
    limit: int = Field(default=50, ge=1, le=250, description="Numero de resultados (1-250)")
    offset: int = Field(default=0, ge=0, description="Numero de resultados a saltar")
    search: Optional[str] = Field(default=None, description="Termino de busqueda")
    searchField: Optional[str] = Field(default=None, description="Campo de busqueda: order_number, customer_name, customer_phone")
    paymentMethod: Optional[str] = Field(default=None, description="Metodo de pago: cash, card, digital")
    status: Optional[str] = Field(default=None, description="Estado: completed, cancelled, pending")
    sortField: str = Field(default="order_date", description="Campo de ordenamiento")
    sortDirection: str = Field(default="desc", description="Direccion: asc, desc")
    dateFrom: Optional[str] = Field(default=None, description="Fecha inicial (YYYY-MM-DD)")
    dateTo: Optional[str] = Field(default=None, description="Fecha final (YYYY-MM-DD)")
    timezone: str = Field(default="America/Bogota", description="Zona horaria para filtros de fecha (ej: America/Bogota, America/Mexico_City, America/New_York)")


class MetricsQueryRequest(BaseModel):
    """Request body para consultar metricas"""
    dateFrom: Optional[str] = Field(default=None, description="Fecha inicial (YYYY-MM-DD)")
    dateTo: Optional[str] = Field(default=None, description="Fecha final (YYYY-MM-DD)")
    timezone: str = Field(default="America/Bogota", description="Zona horaria para filtros de fecha (ej: America/Bogota, America/Mexico_City, America/New_York)")


class SaleDetailRequest(BaseModel):
    """Request body para consultar detalle de venta"""
    orderId: str = Field(description="UUID de la orden")


@router.post("/sales")
async def get_sales(request: Request, body: SalesQueryRequest):
    """
    Obtiene la lista de ventas (ordenes) del tenant autenticado.

    **Autenticacion requerida:**
    - Header `Authorization: Bearer waro_sk_xxx`
    - O header `X-API-Key: waro_sk_xxx`

    **Scope requerido:** `orders:read` o `read`
    """
    return await public_api_service.get_sales_list(
        request,
        limit=body.limit,
        offset=body.offset,
        search=body.search,
        search_field=body.searchField,
        payment_method=body.paymentMethod,
        status=body.status,
        sort_field=body.sortField,
        sort_direction=body.sortDirection,
        date_from=body.dateFrom,
        date_to=body.dateTo,
        timezone=body.timezone
    )


@router.post("/sales/metrics")
async def get_sales_metrics(request: Request, body: MetricsQueryRequest):
    """
    Obtiene metricas de ventas del tenant autenticado.

    **Autenticacion requerida:**
    - Header `Authorization: Bearer waro_sk_xxx`
    - O header `X-API-Key: waro_sk_xxx`

    **Scope requerido:** `orders:read` o `read`
    """
    return await public_api_service.get_sales_metrics(
        request,
        date_from=body.dateFrom,
        date_to=body.dateTo,
        timezone=body.timezone
    )


@router.post("/sales/detail")
async def get_sale(request: Request, body: SaleDetailRequest):
    """
    Obtiene el detalle de una venta especifica incluyendo items y modificadores.

    **Autenticacion requerida:**
    - Header `Authorization: Bearer waro_sk_xxx`
    - O header `X-API-Key: waro_sk_xxx`

    **Scope requerido:** `orders:read` o `read`
    """
    return await public_api_service.get_sale_by_id(request, UUID(body.orderId))

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
    """Request body for sales query"""
    limit: int = Field(default=50, ge=1, le=250, description="Number of results (1-250)")
    offset: int = Field(default=0, ge=0, description="Number of results to skip")
    paymentMethod: Optional[str] = Field(default=None, description="Payment method: cash, card, digital")
    status: Optional[str] = Field(default=None, description="Status: completed, cancelled, pending")
    sortField: str = Field(default="order_date", description="Sort field")
    sortDirection: str = Field(default="desc", description="Direction: asc, desc")
    dateFrom: Optional[str] = Field(default=None, description="Start date (YYYY-MM-DD)")
    dateTo: Optional[str] = Field(default=None, description="End date (YYYY-MM-DD)")
    timezone: str = Field(default="America/Bogota", description="Timezone for date filters (e.g., America/Bogota, America/Mexico_City, America/New_York)")


class MetricsQueryRequest(BaseModel):
    """Request body for metrics query with optional grouping"""
    dateFrom: Optional[str] = Field(default=None, description="Start date (YYYY-MM-DD)")
    dateTo: Optional[str] = Field(default=None, description="End date (YYYY-MM-DD)")
    timezone: str = Field(default="America/Bogota", description="Timezone for date filters")
    groupBy: Optional[str] = Field(default=None, description="Group metrics by: date, weekday, hour, product, payment, ticket")
    limit: int = Field(default=20, ge=1, le=100, description="For product grouping: number of products")
    sortBy: str = Field(default="quantity", description="For product grouping: quantity or revenue")
    ranges: Optional[list] = Field(default=None, description="For ticket grouping: custom price ranges")


class SaleDetailRequest(BaseModel):
    """Request body for sale detail query"""
    orderId: str = Field(description="Order UUID")


class MenuProductsRequest(BaseModel):
    """Request body for menu products query"""
    limit: int = Field(default=50, ge=1, le=250, description="Number of results (1-250)")
    offset: int = Field(default=0, ge=0, description="Number of results to skip")
    categoryId: Optional[str] = Field(default=None, description="Filter by category (UUID)")
    isAvailable: Optional[bool] = Field(default=None, description="Filter by availability")
    includeIngredients: bool = Field(default=True, description="Include product direct ingredients")
    includeRecipeBases: bool = Field(default=True, description="Include associated recipe bases with ingredients")
    includeModifiers: bool = Field(default=True, description="Include associated modifier groups")


class MenuRecipesRequest(BaseModel):
    """Request body for recipe bases query"""
    limit: int = Field(default=50, ge=1, le=250, description="Number of results (1-250)")
    offset: int = Field(default=0, ge=0, description="Number of results to skip")
    isActive: Optional[bool] = Field(default=None, description="Filter by active status")


class MenuModifiersRequest(BaseModel):
    """Request body for modifier groups query"""
    limit: int = Field(default=50, ge=1, le=250, description="Number of results (1-250)")
    offset: int = Field(default=0, ge=0, description="Number of results to skip")


class CustomersListRequest(BaseModel):
    """Request body for customers list query"""
    limit: int = Field(default=50, ge=1, le=250, description="Number of results (1-250)")
    offset: int = Field(default=0, ge=0, description="Number of results to skip")
    search: Optional[str] = Field(default=None, description="Partial name or phone number search (case-insensitive)")
    dateFrom: Optional[str] = Field(default=None, description="Filter by first/last order date start (YYYY-MM-DD)")
    dateTo: Optional[str] = Field(default=None, description="Filter by first/last order date end (YYYY-MM-DD)")
    timezone: str = Field(default="America/Bogota", description="Timezone for date filters (e.g., America/Bogota, America/Mexico_City, America/New_York)")


class CustomerDetailRequest(BaseModel):
    """Request body for customer detail query"""
    customerId: str = Field(description="Customer UUID")


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

    **Parametro groupBy:**
    - null: Metricas generales (default)
    - "weekday": Agrupado por dia de la semana
    - "hour": Agrupado por hora del dia
    - "product": Top productos vendidos
    - "payment": Desglose por metodo de pago
    - "ticket": Distribucion por rangos de precio

    **Autenticacion requerida:**
    - Header `Authorization: Bearer waro_sk_xxx`
    - O header `X-API-Key: waro_sk_xxx`

    **Scope requerido:** `orders:read` o `read`
    """
    return await public_api_service.get_sales_metrics(
        request,
        date_from=body.dateFrom,
        date_to=body.dateTo,
        timezone=body.timezone,
        group_by=body.groupBy,
        limit=body.limit,
        sort_by=body.sortBy,
        ranges=body.ranges
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


@router.post("/menu/products")
async def get_menu_products(request: Request, body: MenuProductsRequest):
    """
    Obtiene la lista de productos del menu con ingredientes, recetas base y modificadores.

    **Autenticacion requerida:**
    - Header `Authorization: Bearer waro_sk_xxx`
    - O header `X-API-Key: waro_sk_xxx`

    **Scope requerido:** `menu:read` o `read`
    """
    return await public_api_service.get_menu_products(
        request,
        limit=body.limit,
        offset=body.offset,
        category_id=body.categoryId,
        is_available=body.isAvailable,
        include_ingredients=body.includeIngredients,
        include_recipe_bases=body.includeRecipeBases,
        include_modifiers=body.includeModifiers
    )


@router.post("/menu/recipes")
async def get_menu_recipes(request: Request, body: MenuRecipesRequest):
    """
    Obtiene la lista de recetas base con sus ingredientes.

    **Autenticacion requerida:**
    - Header `Authorization: Bearer waro_sk_xxx`
    - O header `X-API-Key: waro_sk_xxx`

    **Scope requerido:** `menu:read` o `read`
    """
    return await public_api_service.get_menu_recipes(
        request,
        limit=body.limit,
        offset=body.offset,
        is_active=body.isActive
    )


@router.post("/menu/modifiers")
async def get_menu_modifiers(request: Request, body: MenuModifiersRequest):
    """
    Obtiene la lista de grupos de modificadores con sus opciones e ingredientes.

    **Autenticacion requerida:**
    - Header `Authorization: Bearer waro_sk_xxx`
    - O header `X-API-Key: waro_sk_xxx`

    **Scope requerido:** `menu:read` o `read`
    """
    return await public_api_service.get_menu_modifiers(
        request,
        limit=body.limit,
        offset=body.offset
    )


@router.post("/customers")
async def get_customers(request: Request, body: CustomersListRequest):
    """
    Obtiene la lista de clientes del tenant autenticado, ordenados por total gastado.

    **Autenticacion requerida:**
    - Header `Authorization: Bearer waro_sk_xxx`
    - O header `X-API-Key: waro_sk_xxx`

    **Scope requerido:** `customers:read` o `read`
    """
    return await public_api_service.get_customers_list(
        request,
        limit=body.limit,
        offset=body.offset,
        search=body.search,
        date_from=body.dateFrom,
        date_to=body.dateTo,
        timezone=body.timezone
    )


@router.post("/customers/detail")
async def get_customer_detail(request: Request, body: CustomerDetailRequest):
    """
    Obtiene el perfil, estadisticas y resumen de WaRos de un cliente especifico.

    **Autenticacion requerida:**
    - Header `Authorization: Bearer waro_sk_xxx`
    - O header `X-API-Key: waro_sk_xxx`

    **Scope requerido:** `customers:read` o `read`
    """
    return await public_api_service.get_customer_detail(
        request,
        UUID(body.customerId)
    )



"""
Public API Router
Endpoints for external integrations authenticated via API tokens
"""
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from typing import List, Optional
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
    compareTo: Optional[str] = Field(default=None, description="Comparison mode: previous_period | previous_year | custom. Only applies when groupBy is null.")
    compareFrom: Optional[str] = Field(default=None, description="Custom comparison window start (YYYY-MM-DD). Required when compareTo=custom.")
    compareDateTo: Optional[str] = Field(default=None, description="Custom comparison window end (YYYY-MM-DD). Required when compareTo=custom.")


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
    dateFrom: Optional[str] = Field(default=None, description="Scopes order aggregation start date (YYYY-MM-DD) — does not exclude customers with zero orders in period")
    dateTo: Optional[str] = Field(default=None, description="Scopes order aggregation end date (YYYY-MM-DD)")
    timezone: str = Field(default="America/Bogota", description="Timezone for date filters (e.g., America/Bogota, America/Mexico_City, America/New_York)")
    sortField: str = Field(default="total_spent", description="Sort field: total_spent | order_count | last_order_date | avg_ticket")
    sortDirection: str = Field(default="desc", description="Sort direction: asc | desc")


class CustomerDetailRequest(BaseModel):
    """Request body for customer detail query"""
    customerId: str = Field(description="Customer UUID")
    dateFrom: Optional[str] = Field(default=None, description="Filter order history start date (YYYY-MM-DD)")
    dateTo: Optional[str] = Field(default=None, description="Filter order history end date (YYYY-MM-DD)")
    timezone: str = Field(default="America/Bogota", description="Timezone for date filters (e.g., America/Bogota, America/Mexico_City, America/New_York)")
    limit: int = Field(default=20, ge=1, le=100, description="Number of orders to return (1-100)")
    offset: int = Field(default=0, ge=0, description="Number of orders to skip")


class CustomerMetricsRequest(BaseModel):
    """Request body for customer metrics query"""
    dateFrom: Optional[str] = Field(default=None, description="Start date (YYYY-MM-DD)")
    dateTo: Optional[str] = Field(default=None, description="End date (YYYY-MM-DD)")
    timezone: str = Field(default="America/Bogota", description="Timezone for date filters (e.g., America/Bogota, America/Mexico_City, America/New_York)")
    groupBy: Optional[str] = Field(default=None, description="Time series breakdown: date | weekday | month")
    compareTo: Optional[str] = Field(default=None, description="Comparison mode: previous_period | previous_year | custom. Only applies when groupBy is null.")
    compareFrom: Optional[str] = Field(default=None, description="Custom comparison window start (YYYY-MM-DD). Required when compareTo=custom.")
    compareDateTo: Optional[str] = Field(default=None, description="Custom comparison window end (YYYY-MM-DD). Required when compareTo=custom.")


class CustomerOrdersRequest(BaseModel):
    """Request body for customer order history query"""
    customerId: str = Field(description="Customer UUID")
    dateFrom: Optional[str] = Field(default=None, description="Filter start date (YYYY-MM-DD)")
    dateTo: Optional[str] = Field(default=None, description="Filter end date (YYYY-MM-DD)")
    timezone: str = Field(default="America/Bogota", description="Timezone for date filters (e.g., America/Bogota, America/Mexico_City, America/New_York)")
    limit: int = Field(default=20, ge=1, le=100, description="Number of orders to return (1-100)")
    offset: int = Field(default=0, ge=0, description="Number of orders to skip")
    includeItems: bool = Field(default=False, description="Include line items per order")


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
        ranges=body.ranges,
        compare_to=body.compareTo,
        compare_from=body.compareFrom,
        compare_date_to=body.compareDateTo,
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
        timezone=body.timezone,
        sort_field=body.sortField,
        sort_direction=body.sortDirection
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
        UUID(body.customerId),
        date_from=body.dateFrom,
        date_to=body.dateTo,
        timezone=body.timezone,
        limit=body.limit,
        offset=body.offset
    )


@router.post("/customers/orders")
async def get_customer_orders(request: Request, body: CustomerOrdersRequest):
    """
    Obtiene el historial de pedidos paginado de un cliente especifico, en todas las fuentes (POS, online, manual).

    **Autenticacion requerida:**
    - Header `Authorization: Bearer waro_sk_xxx`
    - O header `X-API-Key: waro_sk_xxx`

    **Scope requerido:** `customers:read` o `read`

    **Parametro includeItems:**
    - false (default): Solo cabeceras de pedido
    - true: Incluye los items de cada pedido (una segunda query batch — sin N+1)

    **Devuelve lista vacia (no 404) cuando el cliente no tiene pedidos.**
    """
    return await public_api_service.get_customer_orders_history(
        request,
        UUID(body.customerId),
        date_from=body.dateFrom,
        date_to=body.dateTo,
        timezone=body.timezone,
        limit=body.limit,
        offset=body.offset,
        include_items=body.includeItems,
    )


@router.post("/customers/metrics")
async def get_customers_metrics(request: Request, body: CustomerMetricsRequest):
    """
    Obtiene metricas agregadas de clientes del tenant autenticado.

    **Parametro groupBy:**
    - null: Solo summary y top clientes (default)
    - "date": Serie por dia calendario
    - "weekday": Serie por dia de la semana (7 entradas)
    - "month": Serie por mes (YYYY-MM)

    **Autenticacion requerida:**
    - Header `Authorization: Bearer waro_sk_xxx`
    - O header `X-API-Key: waro_sk_xxx`

    **Scope requerido:** `customers:read` o `read`
    """
    return await public_api_service.get_customers_metrics(
        request,
        date_from=body.dateFrom,
        date_to=body.dateTo,
        timezone=body.timezone,
        group_by=body.groupBy,
        compare_to=body.compareTo,
        compare_from=body.compareFrom,
        compare_date_to=body.compareDateTo,
    )



# ---------------------------------------------------------------------------
# Analytics request models
# ---------------------------------------------------------------------------

class AnalyticsMenuAnalysisRequest(BaseModel):
    """Request body for BCG menu analysis"""
    dateFrom: Optional[str] = Field(default=None, description="Start date (YYYY-MM-DD)")
    dateTo: Optional[str] = Field(default=None, description="End date (YYYY-MM-DD)")
    limit: int = Field(default=10, ge=1, le=100, description="Max number of products to return")


class AnalyticsFoodCostRequest(BaseModel):
    """Request body for food cost analysis"""
    dateFrom: Optional[str] = Field(default=None, description="Start date (YYYY-MM-DD)")
    dateTo: Optional[str] = Field(default=None, description="End date (YYYY-MM-DD)")
    compareTo: Optional[str] = Field(default=None, description="Comparison mode: previous_period (default) | previous_year | custom")
    compareFrom: Optional[str] = Field(default=None, description="Custom comparison window start (YYYY-MM-DD). Required when compareTo=custom.")
    compareDateTo: Optional[str] = Field(default=None, description="Custom comparison window end (YYYY-MM-DD). Required when compareTo=custom.")


class AnalyticsAlertsRequest(BaseModel):
    """Request body for operational alerts"""
    limit: int = Field(default=10, ge=1, le=100, description="Max number of alerts to return")


class AnalyticsCohortRequest(BaseModel):
    """Request body for cohort retention analysis"""
    dateFrom: Optional[str] = Field(default=None, description="Cohort window start date (YYYY-MM-DD)")
    dateTo: Optional[str] = Field(default=None, description="Cohort window end date (YYYY-MM-DD)")
    period: str = Field(default="weekly", description="Grouping: weekly | monthly")
    periods: int = Field(default=8, ge=1, le=24, description="Number of look-ahead periods (1-24)")
    timezone: str = Field(default="America/Bogota", description="Timezone for date bucketing (e.g., America/Bogota)")


class AnalyticsRFMRequest(BaseModel):
    """Request body for RFM customer segmentation"""
    dateFrom: Optional[str] = Field(default=None, description="Evaluation window start date (YYYY-MM-DD)")
    dateTo: Optional[str] = Field(default=None, description="Evaluation window end date (YYYY-MM-DD)")
    segments: int = Field(default=5, ge=2, le=10, description="Number of scoring tiers (default 5 = quintiles)")
    timezone: str = Field(default="America/Bogota", description="Timezone for date filters (e.g., America/Bogota)")


# ---------------------------------------------------------------------------
# Financial request models
# ---------------------------------------------------------------------------

class FinancialProductsRequest(BaseModel):
    """Request body for financial product analysis"""
    period: int = Field(default=365, ge=1, le=730, description="Analysis period in days")
    category: Optional[str] = Field(default=None, description="Filter by category name")
    minMargin: Optional[int] = Field(default=None, description="Minimum margin percentage filter")
    sortBy: str = Field(default="margin", description="Sort field: margin | revenue | cost | quantity")


# ---------------------------------------------------------------------------
# WaRos request models
# ---------------------------------------------------------------------------

class WarosCustomerSummaryRequest(BaseModel):
    """Request body for customer WaRos wallet summary"""
    profileId: str = Field(description="Customer profile UUID")


class WarosCustomersBalancesRequest(BaseModel):
    """Request body for batch WaRos balances"""
    profileIds: List[str] = Field(description="List of customer profile UUIDs (max 250)")


class WarosEstimateRequest(BaseModel):
    """Request body for WaRos purchase estimate"""
    totalAmount: float = Field(ge=0, description="Purchase total amount")
    customerId: Optional[str] = Field(default=None, description="Customer UUID (for personalized estimate)")


class AnalyticsChurnRiskRequest(BaseModel):
    """Request body for churn risk endpoint"""
    threshold_multiplier: float = Field(default=2.0, ge=1.0, description="Flag customer when inactive N × avg interval (default 2)")
    min_orders: int = Field(default=3, ge=2, description="Minimum completed orders to be included (default 3)")
    limit: int = Field(default=50, ge=1, le=200, description="Page size (max 200)")
    offset: int = Field(default=0, ge=0, description="Pagination offset")


class WarosCustomerHistoryRequest(BaseModel):
    """Request body for paginated WaRos transaction history per customer"""
    profileId: str = Field(description="Customer profile UUID")
    limit: int = Field(default=50, ge=1, le=200, description="Page size (max 200)")
    offset: int = Field(default=0, ge=0, description="Pagination offset")
    transactionType: Optional[str] = Field(default=None, description="Filter by type: earned | manual")


class WarosAnalyticsRequest(BaseModel):
    """Request body for aggregate WaRos analytics"""
    groupBy: str = Field(default="day", description="Grouping: customer | day | week")
    dateFrom: Optional[str] = Field(default=None, description="Start date (YYYY-MM-DD)")
    dateTo: Optional[str] = Field(default=None, description="End date (YYYY-MM-DD)")


# ---------------------------------------------------------------------------
# Analytics endpoints
# ---------------------------------------------------------------------------

@router.post("/analytics/menu-analysis")
async def get_analytics_menu_analysis(request: Request, body: AnalyticsMenuAnalysisRequest):
    """
    Obtiene el analisis BCG del menu (estrellas, vacas, interrogantes, perros).

    **Autenticacion requerida:**
    - Header `Authorization: Bearer waro_sk_xxx`
    - O header `X-API-Key: waro_sk_xxx`

    **Scope requerido:** `analytics:read` o `read`
    """
    return await public_api_service.get_analytics_menu_analysis(
        request,
        date_from=body.dateFrom,
        date_to=body.dateTo,
        limit=body.limit,
    )


@router.post("/analytics/food-cost")
async def get_analytics_food_cost(request: Request, body: AnalyticsFoodCostRequest):
    """
    Obtiene el analisis de food cost por producto.

    **Autenticacion requerida:**
    - Header `Authorization: Bearer waro_sk_xxx`
    - O header `X-API-Key: waro_sk_xxx`

    **Scope requerido:** `analytics:read` o `read`
    """
    return await public_api_service.get_analytics_food_cost(
        request,
        date_from=body.dateFrom,
        date_to=body.dateTo,
        compare_to=body.compareTo,
        compare_from=body.compareFrom,
        compare_date_to=body.compareDateTo,
    )


@router.post("/analytics/alerts")
async def get_analytics_alerts(request: Request, body: AnalyticsAlertsRequest):
    """
    Obtiene alertas operacionales e inventario del tenant.

    **Autenticacion requerida:**
    - Header `Authorization: Bearer waro_sk_xxx`
    - O header `X-API-Key: waro_sk_xxx`

    **Scope requerido:** `analytics:read` o `read`
    """
    return await public_api_service.get_analytics_alerts(
        request,
        limit=body.limit,
    )


@router.post("/analytics/data-quality")
async def get_analytics_data_quality(request: Request):
    """
    Obtiene el reporte de calidad de datos del tenant.

    **Autenticacion requerida:**
    - Header `Authorization: Bearer waro_sk_xxx`
    - O header `X-API-Key: waro_sk_xxx`

    **Scope requerido:** `analytics:read` o `read`
    """
    return await public_api_service.get_analytics_data_quality(request)


@router.post("/analytics/cohort")
async def get_analytics_cohort(request: Request, body: AnalyticsCohortRequest):
    """
    Retorna la matriz de retención por cohorte de adquisición.

    Cada fila = cohorte (semana o mes en que el cliente hizo su primer pedido).
    Cada columna = periodos desde la primera visita.
    Las celdas muestran cuántos clientes del cohorte regresaron en ese periodo y el porcentaje de retención.

    **Parametro `period`:**
    - `weekly` (default): Cohortes por semana ISO
    - `monthly`: Cohortes por mes calendario

    **Parametro `periods`:**
    - Cuántos periodos de look-ahead incluir (default 8 para weekly, recomendado 6 para monthly)

    **Solo incluye clientes identificados** (customer_id IS NOT NULL). Todos los canales de pedido cuentan (POS, online, manual).

    **Autenticacion requerida:**
    - Header `Authorization: Bearer waro_sk_xxx`
    - O header `X-API-Key: waro_sk_xxx`

    **Scope requerido:** `analytics:read` o `read`
    """
    return await public_api_service.get_analytics_cohort(
        request,
        period=body.period,
        periods=body.periods,
        date_from=body.dateFrom,
        date_to=body.dateTo,
        timezone=body.timezone,
    )


@router.post("/analytics/rfm")
async def get_analytics_rfm(request: Request, body: AnalyticsRFMRequest):
    """
    Retorna la segmentación RFM (Recency, Frequency, Monetary) de los clientes del tenant.

    Cada cliente recibe un score R, F y M de 1 a `segments` (default 5 = quintiles)
    y un segmento con nombre legible: Champions, Loyal, At Risk, Hibernating o Lost.

    Solo incluye clientes identificados (customer_id IS NOT NULL) con perfil no anónimo.
    Todos los canales de pedido cuentan (POS, online, manual).

    **Autenticacion requerida:**
    - Header `Authorization: Bearer waro_sk_xxx`
    - O header `X-API-Key: waro_sk_xxx`

    **Scope requerido:** `analytics:read` o `read`
    """
    return await public_api_service.get_analytics_rfm(
        request,
        date_from=body.dateFrom,
        date_to=body.dateTo,
        segments=body.segments,
        timezone=body.timezone,
    )


# ---------------------------------------------------------------------------
# Financial endpoints
# ---------------------------------------------------------------------------

@router.post("/financial/products")
async def get_financial_products(request: Request, body: FinancialProductsRequest):
    """
    Obtiene el analisis financiero de productos (margen, costo, rentabilidad).

    **Autenticacion requerida:**
    - Header `Authorization: Bearer waro_sk_xxx`
    - O header `X-API-Key: waro_sk_xxx`

    **Scope requerido:** `financial:read` o `read`
    """
    return await public_api_service.get_financial_products_analysis(
        request,
        period=body.period,
        category=body.category,
        min_margin=body.minMargin,
        sort_by=body.sortBy,
    )


# ---------------------------------------------------------------------------
# WaRos endpoints
# ---------------------------------------------------------------------------

@router.post("/waros/customer-summary")
async def get_waros_customer_summary(request: Request, body: WarosCustomerSummaryRequest):
    """
    Obtiene el resumen de WaRos (wallet) de un cliente especifico.

    **Autenticacion requerida:**
    - Header `Authorization: Bearer waro_sk_xxx`
    - O header `X-API-Key: waro_sk_xxx`

    **Scope requerido:** `waros:read` o `read`
    """
    return await public_api_service.get_waros_customer_summary(
        request,
        profile_id=UUID(body.profileId),
    )


@router.post("/waros/balances")
async def get_waros_customers_balances(request: Request, body: WarosCustomersBalancesRequest):
    """
    Obtiene los balances de WaRos para multiples clientes en batch.

    **Autenticacion requerida:**
    - Header `Authorization: Bearer waro_sk_xxx`
    - O header `X-API-Key: waro_sk_xxx`

    **Scope requerido:** `waros:read` o `read`
    """
    profile_uuids = [UUID(pid) for pid in body.profileIds]
    return await public_api_service.get_waros_customers_balances(
        request,
        profile_ids=profile_uuids,
    )


@router.post("/waros/estimate")
async def get_waros_estimate(request: Request, body: WarosEstimateRequest):
    """
    Estima cuantos WaRos se ganarian por una compra de determinado monto.

    **Autenticacion requerida:**
    - Header `Authorization: Bearer waro_sk_xxx`
    - O header `X-API-Key: waro_sk_xxx`

    **Scope requerido:** `waros:read` o `read`
    """
    customer_uuid = UUID(body.customerId) if body.customerId else None
    return await public_api_service.get_waros_estimate(
        request,
        total_amount=body.totalAmount,
        customer_id=customer_uuid,
    )


@router.post("/analytics/churn-risk")
async def get_analytics_churn_risk(request: Request, body: AnalyticsChurnRiskRequest):
    """
    Clientes en riesgo de churn basado en su patron de visitas historico.

    Devuelve clientes identificados cuya inactividad supera N veces su intervalo
    promedio de visita personal, ordenados por lifetime value descendente.

    **Autenticacion requerida:**
    - Header `Authorization: Bearer waro_sk_xxx`
    - O header `X-API-Key: waro_sk_xxx`

    **Scope requerido:** `analytics:read` o `read`
    """
    return await public_api_service.get_analytics_churn_risk(
        request,
        threshold_multiplier=body.threshold_multiplier,
        min_orders=body.min_orders,
        limit=body.limit,
        offset=body.offset,
    )


@router.post("/waros/customer-history")
async def get_waros_customer_history(request: Request, body: WarosCustomerHistoryRequest):
    """
    Historial paginado de transacciones de WaRos para un cliente especifico.

    **Autenticacion requerida:**
    - Header `Authorization: Bearer waro_sk_xxx`
    - O header `X-API-Key: waro_sk_xxx`

    **Scope requerido:** `waros:read` o `read`
    """
    profile_uuid = UUID(body.profileId)
    return await public_api_service.get_waros_customer_history(
        request,
        profile_id=profile_uuid,
        limit=body.limit,
        offset=body.offset,
        transaction_type=body.transactionType,
    )


@router.post("/analytics/waros")
async def get_analytics_waros(request: Request, body: WarosAnalyticsRequest):
    """
    Analytics agregado de WaRos: puntos emitidos, canjeados, tasa de redencion y miembros activos.
    Soporta agrupacion por cliente, dia o semana.

    **Autenticacion requerida:**
    - Header `Authorization: Bearer waro_sk_xxx`
    - O header `X-API-Key: waro_sk_xxx`

    **Scope requerido:** `waros:read` o `read`
    """
    valid_groups = {"customer", "day", "week"}
    if body.groupBy not in valid_groups:
        from app.core.exceptions import APIError
        raise APIError(f"groupBy debe ser uno de: {sorted(valid_groups)}", status_code=422)
    return await public_api_service.get_waros_analytics(
        request,
        group_by=body.groupBy,
        date_from=body.dateFrom,
        date_to=body.dateTo,
    )

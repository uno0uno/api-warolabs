from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers import auth, tenants, financial, suppliers, ingredients, purchases, supplier_portal, products, categories, recipe_bases, modifiers, ingredient_purchase_units, customers, pos_cart, orders, inventory, articles, invitations, api_tokens, public_api, v1_ordering, salaries, expenses, public_restaurant, tenant_config, online_cart, online_verification, address_profile, analytics, online_orders, notifications, customer_portal, leads, waros, billing, admin_ingredients, menu, tables, credit, cartera, cierre, payment_methods, accounting, stations, comandas, invoices as invoices_router, support_documents, documents as documents_router, facturacion as facturacion_router, webhooks as webhooks_router
from app.config import settings
from app.core.logging import setup_logging
from app.core.exceptions import api_exception_handler, general_exception_handler, APIError
from app.core.middleware import tenant_detection_middleware, session_validation_middleware, request_logging_middleware
from app.database import DatabasePool

# Initialize logging
setup_logging()

from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

@asynccontextmanager
async def lifespan(app: FastAPI):
    await DatabasePool.create_pool()
    yield
    await DatabasePool.close_pool()

app = FastAPI(
    title="Waro Colombia FastAPI Service",
    version="1.0.0",
    debug=settings.debug,
    docs_url="/docs",
    redirect_slashes=True,  # Explicitly handle trailing slashes
    lifespan=lifespan
)

# Configure cookie authentication for Swagger UI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi

def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title="Waro Colombia FastAPI Service",
        version="1.0.0",
        description="FastAPI service for warocol.com",
        routes=app.routes,
    )
    # Configure cookie authentication
    openapi_schema["components"]["securitySchemes"] = {
        "cookieAuth": {
            "type": "apiKey",
            "in": "cookie", 
            "name": "session-token"
        }
    }
    # Apply security to endpoints that need authentication
    # Magic link endpoints don't need auth (they create the auth)
    # Supplier portal endpoints are public (authenticated via token)
    public_endpoints = [
        "/auth/sign-in-magic-link",
        "/auth/verify-code",
        "/auth/verify",
        "/invitations/accept",
        "/health",
        "/",
        "/supplier-portal"
    ]

    # Prefixes that are public (all paths starting with these)
    # /v1 uses API key authentication instead of cookie
    # /public/restaurant is public for customer menu viewing
    # /online/cart is public for online ordering
    # /online/otp is public for OTP verification
    # /online/customer is public for customer validation
    # /online/addresses is public for address management
    public_prefixes = ["/blog", "/v1", "/public/restaurant", "/online/cart", "/online/otp", "/online/customer", "/online/addresses", "/leads", "/api/webhooks"]

    # Only expose v1 endpoints in Swagger — remove everything else from the schema
    openapi_schema["paths"] = {
        path: methods
        for path, methods in openapi_schema["paths"].items()
        if path.startswith("/v1")
    }

    # Assign tags by path prefix so Swagger groups endpoints into sections
    tag_map = [
        ("/v1/restaurant", "Restaurant"),
        ("/v1/menu",       "Menu"),
        ("/v1/product",    "Menu"),
        ("/v1/sales",      "Sales"),
        ("/v1/customers",  "Customers"),
        ("/v1/analytics",  "Analytics"),
        ("/v1/financial",  "Financial"),
        ("/v1/waros",      "WaRos"),
        ("/v1/cart",       "Ordering"),
        ("/v1/addresses",  "Addresses"),
        ("/v1/otp",        "OTP"),
        ("/v1/customer",   "Customer Auth"),
    ]
    for path, methods in openapi_schema["paths"].items():
        for prefix, tag in tag_map:
            if path.startswith(prefix):
                for method_data in methods.values():
                    if isinstance(method_data, dict):
                        method_data["tags"] = [tag]
                break
    
    app.openapi_schema = openapi_schema
    return app.openapi_schema

app.openapi = custom_openapi

# Exception handlers
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    """
    Custom validation exception handler to handle binary data in errors safely.
    Prevents UnicodeDecodeError when file uploads fail validation.
    """
    errors = exc.errors()
    # Sanitize errors to remove binary data that can't be JSON encoded
    safe_errors = []
    for error in errors:
        safe_error = error.copy()
        if 'input' in safe_error and isinstance(safe_error['input'], (bytes, bytearray)):
            safe_error['input'] = '<binary data>'
        safe_errors.append(safe_error)
            
    return JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder(safe_errors)},
    )

app.add_exception_handler(APIError, api_exception_handler)
app.add_exception_handler(Exception, general_exception_handler)

# CORS middleware - origins from environment variables
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Request logging middleware (first - runs last)
app.middleware("http")(request_logging_middleware)

# Tenant detection middleware
app.middleware("http")(tenant_detection_middleware)

# Session validation middleware
app.middleware("http")(session_validation_middleware)

# Include API routers
app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(tenants.router, prefix="/tenants", tags=["tenants"])
app.include_router(financial.router, prefix="/finance", tags=["financial"])
app.include_router(ingredients.router, prefix="/suppliers/ingredients", tags=["ingredients"])
app.include_router(ingredient_purchase_units.router, prefix="/suppliers/ingredient-purchase-units", tags=["ingredient-purchase-units"])
app.include_router(purchases.router, prefix="/suppliers/purchases", tags=["purchases"])
app.include_router(suppliers.router, prefix="/suppliers/providers", tags=["suppliers"])
app.include_router(supplier_portal.router, prefix="/supplier-portal", tags=["supplier-portal"])
app.include_router(menu.router, prefix="/menu", tags=["menu"])
app.include_router(products.router, prefix="/menu/products", tags=["products"])
app.include_router(categories.router, prefix="/menu/categories", tags=["categories"])
app.include_router(recipe_bases.router, prefix="/menu/recipe-bases", tags=["recipe-bases"])
app.include_router(modifiers.router, prefix="/menu/modifier-groups", tags=["modifiers"])
app.include_router(customers.router, prefix="/customers", tags=["customers"])
app.include_router(pos_cart.router)
app.include_router(online_cart.router)  # Public online ordering cart
app.include_router(online_orders.router)  # Authenticated online orders management
app.include_router(notifications.router)  # Authenticated notifications management
app.include_router(online_verification.router)  # Public OTP verification
app.include_router(online_verification.customer_router)  # Public customer validation
app.include_router(address_profile.router)  # Public address management
app.include_router(orders.router)
app.include_router(inventory.router)
app.include_router(analytics.router)
app.include_router(waros.router)
app.include_router(billing.tenant_router)
app.include_router(articles.router, prefix="/blog", tags=["blog"])
app.include_router(invitations.router, prefix="/invitations", tags=["invitations"])
app.include_router(api_tokens.router, prefix="/api-tokens", tags=["api-tokens"])
app.include_router(salaries.router, prefix="/salaries", tags=["salaries"])
app.include_router(expenses.router, prefix="/finance/expenses", tags=["expenses"])
app.include_router(public_api.router, tags=["public-api"])
app.include_router(v1_ordering.router)           # V1 cart endpoints — API key auth, tenant injected from key
app.include_router(v1_ordering.address_router)   # V1 address endpoints — API key auth
app.include_router(v1_ordering.otp_router)       # V1 OTP endpoints — API key auth + customer_token in verify response
app.include_router(v1_ordering.customer_router_v1)  # V1 customer validate — API key auth
app.include_router(v1_ordering.product_router_v1)   # V1 product detail + modifiers — API key auth
app.include_router(public_restaurant.router, prefix="/public/restaurant", tags=["public-restaurant"])
app.include_router(tenant_config.router, prefix="/api/tenant", tags=["tenant-config"])
app.include_router(stations.router, prefix="/api/stations", tags=["stations"])
app.include_router(tables.router, prefix="/tables", tags=["tables"])
app.include_router(comandas.router, prefix="/api/comandas", tags=["comandas"])
app.include_router(customer_portal.router)  # Customer portal (authenticated via JWT cookie)
app.include_router(leads.router, prefix="/leads", tags=["leads"])  # Public lead capture
app.include_router(admin_ingredients.router)  # /admin/ingredients — hierarchy CRUD (issue #259)
app.include_router(credit.router)   # /credit — credit sales payments API (issue #294)
app.include_router(cartera.router)  # /cartera — portfolio & aging report API (issue #308)
app.include_router(cierre.router)   # /cierre — daily accounting close (issue #311)
app.include_router(payment_methods.finanzas_router)  # /finanzas/metodos-pago — payment method groups & methods (issue #331)
app.include_router(accounting.router, prefix="/accounting", tags=["accounting"])  # /accounting — chart of accounts CRUD (issue #375)
app.include_router(payment_methods.pos_router)        # /pos/payment-methods — POS read-only (issue #331)
app.include_router(invoices_router.router)            # /api/invoices/... — credit-note, debit-note, events (issue #129)
app.include_router(support_documents.router)          # /api/support-documents/... — support docs (issue #129)
app.include_router(documents_router.router)           # /api/documents/... — document management (issue #129)
app.include_router(facturacion_router.acquirer_router)  # /api/acquirer — acquirer lookup (issue #129)
app.include_router(facturacion_router.catalog_router)   # /api/facturacion/catalog/... — DIAN catalogs (issue #129)
app.include_router(facturacion_router.payroll_router)   # /api/payroll/... — electronic payroll (issue #129)
app.include_router(webhooks_router.router)              # /api/webhooks/matias — Matias webhook bridge

@app.get("/")
async def root():
    return {
        "message": "Waro Colombia FastAPI Service", 
        "version": "1.0.0",
        "database": settings.db_name,
        "environment": settings.environment
    }

@app.get("/health")
async def health():
    return {
        "status": "healthy", 
        "database": settings.db_name,
        "host": settings.db_host
    }

# Auto-start server if run directly
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug
    )
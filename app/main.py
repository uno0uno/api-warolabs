from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers import auth, tenants, financial, suppliers, ingredients, purchases, supplier_portal, products, categories, recipe_bases, modifiers, combos, ingredient_purchase_units, customers, pos_cart, orders, inventory, articles, invitations, api_tokens, public_api, salaries, expenses, public_restaurant, tenant_config, online_cart, online_verification, address_profile, analytics, online_orders, notifications, customer_portal
from app.config import settings
from app.core.logging import setup_logging
from app.core.exceptions import api_exception_handler, general_exception_handler, APIError
from app.core.middleware import tenant_detection_middleware, session_validation_middleware, request_logging_middleware

# Initialize logging
setup_logging()

from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

app = FastAPI(
    title="Waro Colombia FastAPI Service",
    version="1.0.0",
    debug=settings.debug,
    docs_url="/docs",
    redirect_slashes=True  # Explicitly handle trailing slashes
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
    public_prefixes = ["/blog", "/v1", "/public/restaurant", "/online/cart", "/online/otp", "/online/customer", "/online/addresses", "/api/customer"]

    for path in openapi_schema["paths"]:
        # Skip public endpoints
        if path in public_endpoints:
            continue
        # Skip public prefixes (blog endpoints are public)
        if any(path.startswith(prefix) for prefix in public_prefixes):
            continue
            
        for method in openapi_schema["paths"][path]:
            if method in ["get", "post", "put", "delete", "patch"]:
                openapi_schema["paths"][path][method]["security"] = [{"cookieAuth": []}]
    
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
app.include_router(products.router, prefix="/menu/products", tags=["products"])
app.include_router(categories.router, prefix="/menu/categories", tags=["categories"])
app.include_router(recipe_bases.router, prefix="/menu/recipe-bases", tags=["recipe-bases"])
app.include_router(modifiers.router, prefix="/menu/modifier-groups", tags=["modifiers"])
app.include_router(combos.router, prefix="/menu/combos", tags=["combos"])
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
app.include_router(articles.router, prefix="/blog", tags=["blog"])
app.include_router(invitations.router, prefix="/invitations", tags=["invitations"])
app.include_router(api_tokens.router, prefix="/api-tokens", tags=["api-tokens"])
app.include_router(salaries.router, prefix="/salaries", tags=["salaries"])
app.include_router(expenses.router, prefix="/finance/expenses", tags=["expenses"])
app.include_router(public_api.router, tags=["public-api"])
app.include_router(public_restaurant.router, prefix="/public/restaurant", tags=["public-restaurant"])
app.include_router(tenant_config.router, prefix="/api/tenant", tags=["tenant-config"])
app.include_router(customer_portal.router)  # Customer portal (authenticated via JWT cookie)

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
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers import auth, tenants, financial
from app.config import settings
from app.core.logging import setup_logging
from app.core.exceptions import api_exception_handler, general_exception_handler, APIError

# Initialize logging
setup_logging()

from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

app = FastAPI(
    title="Warolabs FastAPI Service", 
    version="1.0.0",
    debug=settings.debug,
    docs_url="/docs"
)

# Configure cookie authentication for Swagger UI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi

def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title="Warolabs FastAPI Service",
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
    # Apply security to all endpoints
    for path in openapi_schema["paths"]:
        for method in openapi_schema["paths"][path]:
            if method in ["get", "post", "put", "delete", "patch"]:
                openapi_schema["paths"][path][method]["security"] = [{"cookieAuth": []}]
    
    app.openapi_schema = openapi_schema
    return app.openapi_schema

app.openapi = custom_openapi

# Exception handlers
app.add_exception_handler(APIError, api_exception_handler)
app.add_exception_handler(Exception, general_exception_handler)

# CORS middleware for warocol.com compatibility
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8080",  # warocol.com development
        "https://warocol.com",    # warocol.com production
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API routers
app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(tenants.router, prefix="/tenants", tags=["tenants"])
app.include_router(financial.router, prefix="/finance", tags=["financial"])

@app.get("/")
async def root():
    return {
        "message": "Warolabs FastAPI Service", 
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
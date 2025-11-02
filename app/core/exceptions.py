from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
import logging
from typing import Dict, Any
from app.core.logging import log_request_context

logger = logging.getLogger(__name__)

class APIError(Exception):
    """Base API exception compatible with warolabs.com error patterns"""
    
    def __init__(self, message: str, status_code: int = 500, details: Dict[str, Any] = None):
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        super().__init__(self.message)

class AuthenticationError(APIError):
    """Authentication related errors"""
    
    def __init__(self, message: str = "Authentication required", details: Dict[str, Any] = None):
        super().__init__(message, 401, details)

class AuthorizationError(APIError):
    """Authorization related errors"""
    
    def __init__(self, message: str = "Access denied", details: Dict[str, Any] = None):
        super().__init__(message, 403, details)

class TenantError(APIError):
    """Tenant validation errors"""
    
    def __init__(self, message: str = "Invalid tenant", details: Dict[str, Any] = None):
        super().__init__(message, 404, details)

class DatabaseError(APIError):
    """Database operation errors"""
    
    def __init__(self, message: str = "Database operation failed", details: Dict[str, Any] = None):
        super().__init__(message, 500, details)

async def api_exception_handler(request: Request, exc: APIError):
    """Handle custom API exceptions with logging"""
    
    # Extract tenant from request if available
    tenant = getattr(request.state, 'tenant', 'unknown')
    
    # Create log context
    context = log_request_context(tenant)
    context.update({
        "error_type": exc.__class__.__name__,
        "status_code": exc.status_code,
        "path": str(request.url.path),
        "method": request.method
    })
    
    if exc.status_code >= 500:
        logger.error(f"API Error: {exc.message}", extra={"context": context})
    else:
        logger.warning(f"API Error: {exc.message}", extra={"context": context})
    
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": True,
            "message": exc.message,
            "details": exc.details,
            "timestamp": context["timestamp"]
        }
    )

async def general_exception_handler(request: Request, exc: Exception):
    """Handle unexpected exceptions"""
    
    tenant = getattr(request.state, 'tenant', 'unknown')
    context = log_request_context(tenant)
    context.update({
        "error_type": exc.__class__.__name__,
        "path": str(request.url.path),
        "method": request.method
    })
    
    logger.error(f"Unexpected error: {str(exc)}", extra={"context": context}, exc_info=True)
    
    return JSONResponse(
        status_code=500,
        content={
            "error": True,
            "message": "Internal server error",
            "timestamp": context["timestamp"]
        }
    )
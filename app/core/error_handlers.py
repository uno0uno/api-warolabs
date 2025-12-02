"""
Global Error Handlers for FastAPI
Catches all exceptions and sends notifications to Discord
"""
from fastapi import Request, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
import logging
from typing import Union
from app.services.discord_error_notifier import error_notifier

logger = logging.getLogger(__name__)


async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Global exception handler that catches all unhandled exceptions
    and sends them to Discord
    """
    # Extract request info
    request_info = {
        "method": request.method,
        "url": str(request.url),
        "client_host": request.client.host if request.client else "unknown",
        "user_agent": request.headers.get("user-agent", "unknown")
    }

    # Get user info if available (from headers or session)
    context = {}

    # Try to get tenant/user from headers or path
    if "tenant-id" in request.headers:
        context["tenant_id"] = request.headers["tenant-id"]
    if "user-id" in request.headers:
        context["user_id"] = request.headers["user-id"]

    # Log the error
    logger.error(f"Unhandled exception: {type(exc).__name__}: {str(exc)}")
    logger.exception(exc)

    # Send to Discord (don't await to avoid blocking the response)
    try:
        await error_notifier.send_error(
            error=exc,
            context=context,
            request_info=request_info
        )
    except Exception as notify_error:
        logger.error(f"Failed to notify Discord about error: {notify_error}")

    # Return error response
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "success": False,
            "detail": "Internal server error",
            "error_type": type(exc).__name__
        }
    )


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """
    Handler for HTTPException (400, 404, etc.)
    Only sends to Discord for 500 errors
    """
    # Only notify Discord for server errors (5xx)
    if exc.status_code >= 500:
        request_info = {
            "method": request.method,
            "url": str(request.url),
            "client_host": request.client.host if request.client else "unknown",
            "user_agent": request.headers.get("user-agent", "unknown")
        }

        context = {
            "status_code": exc.status_code
        }

        try:
            await error_notifier.send_error(
                error=exc,
                context=context,
                request_info=request_info
            )
        except Exception as notify_error:
            logger.error(f"Failed to notify Discord about HTTP error: {notify_error}")

    # Return the HTTP exception response
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "detail": exc.detail
        }
    )


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """
    Handler for validation errors (422)
    These are client errors, so we don't send them to Discord
    """
    logger.warning(f"Validation error on {request.method} {request.url}: {exc.errors()}")

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "success": False,
            "detail": "Validation error",
            "errors": exc.errors()
        }
    )


async def starlette_http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """
    Handler for Starlette HTTP exceptions
    """
    # Only notify Discord for server errors (5xx)
    if exc.status_code >= 500:
        request_info = {
            "method": request.method,
            "url": str(request.url),
            "client_host": request.client.host if request.client else "unknown",
            "user_agent": request.headers.get("user-agent", "unknown")
        }

        context = {
            "status_code": exc.status_code
        }

        try:
            await error_notifier.send_error(
                error=exc,
                context=context,
                request_info=request_info
            )
        except Exception as notify_error:
            logger.error(f"Failed to notify Discord about Starlette HTTP error: {notify_error}")

    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "detail": exc.detail
        }
    )

import logging
import time
from typing import Optional, Dict, Any
from urllib.parse import urlparse
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from app.database import get_db_connection

logger = logging.getLogger(__name__)

class SessionContext:
    """Session context object"""
    def __init__(self, session_data: Optional[Dict[str, Any]] = None):
        if session_data:
            self.user_id = session_data['user_id']
            self.tenant_id = session_data['tenant_id']
            self.email = session_data['email']
            self.name = session_data['name']
            self.expires_at = session_data['expires_at']
            self.is_active = session_data['is_active']
            self.is_valid = True
        else:
            self.user_id = None
            self.tenant_id = None
            self.email = None
            self.name = None
            self.expires_at = None
            self.is_active = False
            self.is_valid = False
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'user_id': self.user_id,
            'tenant_id': self.tenant_id,
            'email': self.email,
            'name': self.name,
            'expires_at': self.expires_at,
            'is_active': self.is_active,
            'is_valid': self.is_valid
        }

class TenantContext:
    """Tenant context object"""
    def __init__(self, tenant_data: Optional[Dict[str, Any]] = None):
        if tenant_data:
            self.tenant_id = tenant_data['tenant_id']
            self.tenant_name = tenant_data['tenant_name']
            self.tenant_slug = tenant_data['tenant_slug']
            self.tenant_email = tenant_data['tenant_email']
            self.site = tenant_data['site']
            self.brand_name = tenant_data['brand_name']
            self.is_active = tenant_data['is_active']
            self.is_valid = True
        else:
            self.tenant_id = None
            self.tenant_name = None
            self.tenant_slug = None
            self.tenant_email = None
            self.site = None
            self.brand_name = None
            self.is_active = False
            self.is_valid = False
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'tenant_id': self.tenant_id,
            'tenant_name': self.tenant_name,
            'tenant_slug': self.tenant_slug,
            'tenant_email': self.tenant_email,
            'site': self.site,
            'brand_name': self.brand_name,
            'is_active': self.is_active,
            'is_valid': self.is_valid
        }

async def tenant_detection_middleware(request: Request, call_next):
    """
    Middleware to detect and validate tenant from request origin
    Sets request.state.tenant_context for use in endpoints
    """
    try:
        # Skip tenant detection for health checks, docs and root endpoint
        if request.url.path in ['/health', '/docs', '/redoc', '/openapi.json', '/']:
            response = await call_next(request)
            return response
        
        # Detect requesting site from headers
        referer = request.headers.get('referer', '')
        origin = request.headers.get('origin', '')
        host = request.headers.get('host', '')
        
        # Debug: Log all relevant headers for CloudFront troubleshooting
        debug_headers = {
            'referer': referer,
            'origin': origin, 
            'host': host,
            'x-forwarded-host': request.headers.get('x-forwarded-host', ''),
            'x-original-host': request.headers.get('x-original-host', ''),
            'x-forwarded-for': request.headers.get('x-forwarded-for', ''),
            'cloudfront-viewer-country': request.headers.get('cloudfront-viewer-country', ''),
            'user-agent': request.headers.get('user-agent', '')[:100] + "..." if len(request.headers.get('user-agent', '')) > 100 else request.headers.get('user-agent', '')
        }
        logger.info(f"🌐 Headers debug: {debug_headers}")
        
        requesting_site = None
        
        # Try to extract site from referer first
        if referer:
            url = urlparse(referer)
            requesting_site = url.netloc  # Use netloc to include port
        elif origin:
            url = urlparse(origin)
            requesting_site = url.netloc  # Use netloc to include port
        elif host:
            # Use host header as fallback
            requesting_site = host
        
        if not requesting_site:
            logger.warning("No requesting site detected from headers")
            request.state.tenant_context = TenantContext()
            return JSONResponse(
                status_code=400,
                content={"error": "Unable to determine requesting site"}
            )
        
        # Handle development environment - map localhost ports to actual sites
        if 'localhost' in requesting_site or '127.0.0.1' in requesting_site:
            if ':8080' in requesting_site or requesting_site == 'localhost:8080':
                requesting_site = 'warocol.com'
            elif ':4000' in requesting_site or requesting_site == 'localhost:4000':
                requesting_site = 'warolabs.com'
            else:
                logger.warning(f"Unknown localhost port: {requesting_site}")
                request.state.tenant_context = TenantContext()
                return JSONResponse(
                    status_code=400,
                    content={"error": f"Unknown development site: {requesting_site}"}
                )
        
        # Handle api subdomain - map api.warolabs.com to warolabs.com
        if requesting_site == 'api.warolabs.com':
            requesting_site = 'warolabs.com'
        
        # Query database for tenant site configuration
        async with get_db_connection() as conn:
            tenant_query = """
                SELECT 
                    ts.tenant_id,
                    ts.site,
                    ts.brand_name,
                    ts.is_active,
                    t.name as tenant_name,
                    t.slug as tenant_slug,
                    t.email as tenant_email
                FROM tenant_sites ts
                JOIN tenants t ON ts.tenant_id = t.id
                WHERE ts.site = $1 AND ts.is_active = true
                LIMIT 1
            """
            
            tenant_data = await conn.fetchrow(tenant_query, requesting_site)
            
            if not tenant_data:
                request.state.tenant_context = TenantContext()
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": "Access denied",
                        "message": f"Site '{requesting_site}' is not authorized to access this API"
                    }
                )
            
            # Create tenant context
            tenant_context = TenantContext(dict(tenant_data))
            request.state.tenant_context = tenant_context
        
        # Continue to endpoint
        response = await call_next(request)
        return response
        
    except Exception as e:
        logger.error(f"❌ Tenant detection middleware error: {e}", exc_info=True)
        request.state.tenant_context = TenantContext()
        return JSONResponse(
            status_code=500,
            content={"error": "Internal server error during tenant detection"}
        )

def get_tenant_context(request: Request) -> TenantContext:
    """
    Helper function to get tenant context from request
    """
    return getattr(request.state, 'tenant_context', TenantContext())

def require_valid_tenant(request: Request) -> TenantContext:
    """
    Helper function that raises error if no valid tenant context
    """
    tenant_context = get_tenant_context(request)
    if not tenant_context.is_valid:
        from app.core.exceptions import ValidationError
        raise ValidationError("Valid tenant context required")
    return tenant_context

async def session_validation_middleware(request: Request, call_next):
    """
    Middleware to validate session for protected endpoints
    Sets request.state.session_context for use in endpoints
    """
    try:
        # Skip session validation for public endpoints
        path = request.url.path
        public_endpoints = [
            '/docs', '/openapi.json', '/health',
            '/auth/sign-in-magic-link', '/auth/verify-code', '/auth/verify'
        ]
        
        # Handle exact root path separately
        if path == '/' or any(path.startswith(endpoint) for endpoint in public_endpoints):
            request.state.session_context = SessionContext()
            return await call_next(request)
        
        # Get session token from cookies
        session_token = request.cookies.get("session-token")
        
        if not session_token:
            request.state.session_context = SessionContext()
            return await call_next(request)
        
        # Validate session in database
        async with get_db_connection() as conn:
            session_query = """
                SELECT s.user_id, s.tenant_id, s.expires_at, s.is_active,
                       p.email, p.name
                FROM sessions s
                JOIN profile p ON s.user_id = p.id
                WHERE s.id = $1 
                  AND s.expires_at > NOW()
                  AND s.is_active = true
                LIMIT 1
            """
            session_result = await conn.fetchrow(session_query, session_token)
            
            if not session_result:
                request.state.session_context = SessionContext()
                return await call_next(request)
            
            # Update last activity
            await conn.execute(
                'UPDATE sessions SET last_activity_at = NOW() WHERE id = $1',
                session_token
            )
            
            # Create session context
            session_data = {
                'user_id': session_result['user_id'],
                'tenant_id': session_result['tenant_id'],
                'email': session_result['email'],
                'name': session_result['name'],
                'expires_at': session_result['expires_at'],
                'is_active': session_result['is_active']
            }
            
            request.state.session_context = SessionContext(session_data)
            
        return await call_next(request)
        
    except Exception as e:
        logger.error(f"Session validation error: {e}")
        request.state.session_context = SessionContext()
        return await call_next(request)

def get_session_context(request: Request) -> SessionContext:
    """
    Helper function to get session context from request
    """
    return getattr(request.state, 'session_context', SessionContext())

def require_valid_session(request: Request) -> SessionContext:
    """
    Helper function that raises error if no valid session context
    """
    session_context = get_session_context(request)
    if not session_context.is_valid:
        from app.core.exceptions import AuthenticationError
        raise AuthenticationError("Valid session required")
    return session_context

async def request_logging_middleware(request: Request, call_next):
    """
    Simple request logging middleware for production monitoring
    Logs endpoint calls with basic info
    """
    start_time = time.time()
    
    # Get basic request info
    method = request.method
    path = request.url.path
    
    # Get tenant and user info if available
    tenant_name = getattr(getattr(request.state, 'tenant_context', None), 'tenant_name', 'unknown')
    session_context = getattr(request.state, 'session_context', None)
    user_id = getattr(session_context, 'user_id', 'anonymous') if session_context else 'anonymous'
    
    # Process request
    response = await call_next(request)
    
    # Calculate duration
    duration = round((time.time() - start_time) * 1000, 2)  # milliseconds
    
    # Log endpoint call
    logger.info(f"API {method} {path} | {response.status_code} | {duration}ms | {tenant_name} | {user_id}")
    
    return response
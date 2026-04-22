import logging
import time
from typing import Optional, Dict, Any
from urllib.parse import urlparse
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from app.database import get_db_connection
from app.config import settings

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


def extract_api_key(request: Request) -> Optional[str]:
    """
    Extract API key from request headers.
    Supports:
    - Authorization: Bearer waro_sk_xxx
    - X-API-Key: waro_sk_xxx
    """
    # Try Authorization header first
    auth_header = request.headers.get('authorization', '')
    if auth_header.startswith('Bearer '):
        token = auth_header[7:]  # Remove 'Bearer ' prefix
        if token.startswith('waro_'):
            return token

    # Try X-API-Key header
    api_key_header = request.headers.get('x-api-key', '')
    if api_key_header.startswith('waro_'):
        return api_key_header

    return None


async def tenant_detection_middleware(request: Request, call_next):
    """
    Middleware to detect and validate tenant from request origin or API key
    Sets request.state.tenant_context for use in endpoints
    """
    try:
        # Skip tenant detection for health checks, docs and root endpoint
        if request.url.path in ['/health', '/docs', '/redoc', '/openapi.json', '/']:
            response = await call_next(request)
            return response

        # Skip tenant detection for public restaurant endpoints (they use slug-based lookup)
        if request.url.path.startswith('/public/restaurant'):
            request.state.tenant_context = TenantContext()
            response = await call_next(request)
            return response

        # Skip tenant detection for KDS public GET endpoints (UUID-secured, no session/tenant required)
        kds_public_path = (
            request.method == 'GET' and (
                request.url.path.startswith('/api/stations/') or
                request.url.path.startswith('/api/comandas')
            )
        )
        if kds_public_path:
            request.state.tenant_context = TenantContext()
            response = await call_next(request)
            return response

        # Check for API key authentication - if present, get tenant from token
        api_key = extract_api_key(request)
        if api_key:
            try:
                from app.services.api_tokens_service import validate_api_key
                api_key_data = await validate_api_key(api_key)
                if api_key_data:
                    # Get tenant info from database using token's tenant_id
                    async with get_db_connection(use_transaction=False) as conn:
                        tenant_query = """
                            SELECT
                                t.id as tenant_id,
                                t.name as tenant_name,
                                t.slug as tenant_slug,
                                t.email as tenant_email,
                                ts.site,
                                ts.brand_name,
                                ts.is_active
                            FROM tenants t
                            LEFT JOIN tenant_sites ts ON t.id = ts.tenant_id AND ts.is_active = true
                            WHERE t.id = $1
                            LIMIT 1
                        """
                        from uuid import UUID
                        tenant_data = await conn.fetchrow(tenant_query, UUID(api_key_data['tenant_id']))
                        if tenant_data:
                            request.state.tenant_context = TenantContext(dict(tenant_data))
                            logger.debug(f"Tenant context set from API key: {tenant_data['tenant_name']}")
                            response = await call_next(request)
                            return response
            except Exception as e:
                logger.warning(f"API key tenant detection failed: {e}")
                # Fall through to header-based detection

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
        # Headers debug removed - keeping logs clean
        
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
            # Fallback: Try to infer tenant from session token if available
            session_token = request.cookies.get("session-token")
            if session_token:
                logger.info(f"🔍 No origin header, attempting tenant inference from session: {session_token}")
                try:
                    async with get_db_connection(use_transaction=False) as conn:
                        session_tenant_query = """
                            SELECT ts.site, ts.tenant_id, ts.brand_name, ts.is_active,
                                   t.name as tenant_name, t.slug as tenant_slug, t.email as tenant_email
                            FROM sessions s
                            JOIN tenant_sites ts ON s.tenant_id = ts.tenant_id
                            JOIN tenants t ON ts.tenant_id = t.id
                            WHERE s.id = $1 AND s.expires_at > NOW() AND s.is_active = true
                              AND ts.is_active = true
                            LIMIT 1
                        """
                        session_tenant_result = await conn.fetchrow(session_tenant_query, session_token)
                        if session_tenant_result:
                            logger.info(f"✅ Inferred tenant from session: {session_tenant_result['tenant_name']}")
                            requesting_site = session_tenant_result['site']
                        else:
                            logger.warning("Session not found or expired for tenant inference")
                except Exception as e:
                    logger.warning(f"Failed to infer tenant from session: {e}")
            
            if not requesting_site:
                logger.warning("No requesting site detected from headers or session")
                request.state.tenant_context = TenantContext()
                return JSONResponse(
                    status_code=400,
                    content={"error": "Unable to determine requesting site"}
                )
        
        # Handle development environment - map localhost and local network IPs to actual sites
        is_local_dev = (
            'localhost' in requesting_site or
            '127.0.0.1' in requesting_site or
            requesting_site.startswith('192.168.') or
            requesting_site.startswith('10.') or
            requesting_site.startswith('172.')
        )

        if is_local_dev:
            # Parse localhost mapping from environment variables
            localhost_mappings = {}
            port_mappings = {}  # Map ports to tenants

            if settings.localhost_mapping:
                for mapping in settings.localhost_mapping.split(','):
                    if '=' in mapping:
                        localhost, tenant = mapping.strip().split('=')
                        localhost_mappings[localhost] = tenant

                        # Extract port for local IP mapping
                        if ':' in localhost:
                            port = localhost.split(':')[1]
                            port_mappings[port] = tenant

            # Check if requesting site is in direct mappings
            if requesting_site in localhost_mappings:
                requesting_site = localhost_mappings[requesting_site]
            # For local IPs, try to match by port
            elif ':' in requesting_site:
                port = requesting_site.split(':')[1]
                if port in port_mappings:
                    requesting_site = port_mappings[port]
                    logger.info(f"Mapped local IP port {port} to {requesting_site}")
                else:
                    logger.warning(f"Unknown local network port: {requesting_site}")
                    request.state.tenant_context = TenantContext()
                    return JSONResponse(
                        status_code=400,
                        content={"error": f"Unknown development site port: {requesting_site}"}
                    )
            else:
                logger.warning(f"Unknown localhost configuration: {requesting_site}")
                request.state.tenant_context = TenantContext()
                return JSONResponse(
                    status_code=400,
                    content={"error": f"Unknown development site: {requesting_site}"}
                )
        
        # Handle api subdomain - map api.warolabs.com to warolabs.com
        if requesting_site == 'api.warolabs.com':
            requesting_site = 'warolabs.com'
        
        # Query database for tenant site configuration (read-only, no transaction needed)
        async with get_db_connection(use_transaction=False) as conn:
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
                WHERE ts.site::text = $1 AND ts.is_active = true
                LIMIT 1
            """

            try:
                tenant_data = await conn.fetchrow(tenant_query, requesting_site)
            except Exception as e:
                # Handle invalid enum values or other DB errors
                logger.warning(f"Tenant lookup failed for site '{requesting_site}': {e}")
                tenant_data = None

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

class ApiKeyContext:
    """API Key authentication context"""
    def __init__(self, api_key_data: Optional[Dict[str, Any]] = None):
        if api_key_data:
            self.token_id = api_key_data.get('token_id')
            self.tenant_id = api_key_data.get('tenant_id')
            self.scopes = api_key_data.get('scopes', [])
            self.is_valid = True
        else:
            self.token_id = None
            self.tenant_id = None
            self.scopes = []
            self.is_valid = False

    def has_scope(self, scope: str) -> bool:
        """Check if API key has a specific scope"""
        if 'write' in self.scopes:
            return True  # write implies all permissions
        if 'read' in self.scopes and scope.endswith(':read'):
            return True
        return scope in self.scopes

    def to_dict(self) -> Dict[str, Any]:
        return {
            'token_id': self.token_id,
            'tenant_id': self.tenant_id,
            'scopes': self.scopes,
            'is_valid': self.is_valid
        }


async def session_validation_middleware(request: Request, call_next):
    """
    Middleware to validate session or API key for protected endpoints
    Sets request.state.session_context for use in endpoints
    Also sets request.state.api_key_context if using API key auth
    """
    try:
        # Skip session validation for public endpoints
        path = request.url.path
        public_endpoints = [
            '/docs', '/openapi.json', '/health',
            '/auth/sign-in-magic-link', '/auth/verify-code', '/auth/verify'
        ]

        # Public prefixes (no session required)
        # /api/stations GET-only: KDS screen fetches station metadata by UUID (no session, UUID-secured)
        # /api/comandas GET-only: KDS screen polls active comandas by station_id (no session, UUID-secured)
        public_prefixes = ['/blog', '/supplier-portal', '/public/restaurant']
        kds_public = (
            request.method == 'GET' and (
                path.startswith('/api/stations/') or
                path.startswith('/api/comandas')
            )
        )

        # Handle exact root path separately
        if path == '/' or any(path.startswith(endpoint) for endpoint in public_endpoints) or any(path.startswith(prefix) for prefix in public_prefixes) or kds_public:
            request.state.session_context = SessionContext()
            request.state.api_key_context = ApiKeyContext()
            return await call_next(request)

        # First, check for API key authentication
        api_key = extract_api_key(request)
        if api_key:
            try:
                from app.services.api_tokens_service import validate_api_key
                api_key_data = await validate_api_key(api_key)
                if api_key_data:
                    request.state.api_key_context = ApiKeyContext(api_key_data)
                    # Create a pseudo-session context for API key auth
                    # This allows existing code to work with API key auth
                    pseudo_session = {
                        'user_id': None,  # API keys don't have a user
                        'tenant_id': api_key_data['tenant_id'],
                        'email': None,
                        'name': f"API Key ({api_key[:16]}...)",
                        'expires_at': None,
                        'is_active': True
                    }
                    request.state.session_context = SessionContext(pseudo_session)
                    logger.debug(f"✅ API key authenticated: {api_key[:16]}... for tenant {api_key_data['tenant_id']}")
                    return await call_next(request)
                else:
                    logger.warning(f"❌ Invalid API key: {api_key[:16]}...")
                    request.state.api_key_context = ApiKeyContext()
            except Exception as e:
                logger.error(f"❌ API key validation error: {e}")
                request.state.api_key_context = ApiKeyContext()
        else:
            request.state.api_key_context = ApiKeyContext()

        # Fall back to session-based authentication
        from app.core.security import get_session_from_request
        try:
            session_data = await get_session_from_request(request)
            if session_data:
                request.state.session_context = SessionContext(session_data)
                logger.debug(f"✅ Session context set for user: {session_data.get('user_id')}, tenant: {session_data.get('tenant_id')}")
            else:
                logger.debug(f"⚠️ No session data returned for path: {path}")
                request.state.session_context = SessionContext()
        except Exception as e:
            # No valid session found
            logger.warning(f"❌ Session validation error for path {path}: {e}")
            request.state.session_context = SessionContext()

        return await call_next(request)

    except Exception as e:
        logger.error(f"Session validation error: {e}")
        request.state.session_context = SessionContext()
        request.state.api_key_context = ApiKeyContext()
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


def get_api_key_context(request: Request) -> ApiKeyContext:
    """
    Helper function to get API key context from request
    """
    return getattr(request.state, 'api_key_context', ApiKeyContext())


def require_api_key_scope(request: Request, scope: str) -> ApiKeyContext:
    """
    Helper function that raises error if API key doesn't have required scope
    """
    api_key_context = get_api_key_context(request)
    if not api_key_context.is_valid:
        from app.core.exceptions import AuthenticationError
        raise AuthenticationError("Valid API key required")
    if not api_key_context.has_scope(scope):
        from app.core.exceptions import AuthorizationError
        raise AuthorizationError(f"API key missing required scope: {scope}")
    return api_key_context


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
    # Simple endpoint logging with timestamp
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    query = f"?{request.url.query}" if request.url.query else ""
    logger.info(f"{timestamp} | {method} {path}{query}")
    
    return response
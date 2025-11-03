import logging
from typing import Optional, Dict, Any
from urllib.parse import urlparse
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from app.database import get_db_connection

logger = logging.getLogger(__name__)

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
        # Skip tenant detection for health checks and docs
        if request.url.path in ['/health', '/docs', '/redoc', '/openapi.json']:
            response = await call_next(request)
            return response
        
        # Detect requesting site from headers
        referer = request.headers.get('referer', '')
        origin = request.headers.get('origin', '')
        host = request.headers.get('host', '')
        
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
        
        logger.info(f"🌐 Detected requesting site: {requesting_site}")
        
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
                logger.warning(f"❌ Site '{requesting_site}' not found or inactive in tenant_sites")
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
            
            logger.info(f"✅ Tenant context set: {tenant_context.tenant_name} ({tenant_context.site})")
        
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
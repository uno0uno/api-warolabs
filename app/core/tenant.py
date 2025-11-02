from fastapi import Request, HTTPException
from app.database import get_db_connection
from app.core.security import detect_tenant_from_headers
from app.config import settings
import json
from pathlib import Path

async def detect_and_validate_tenant(request: Request) -> str:
    """
    Port EXACT tenant detection logic from warolabs.com/server/api/events/index.post.js
    
    This function replicates the exact multi-tenant detection logic used in warolabs.com
    """
    
    headers = detect_tenant_from_headers(request)
    potential_sites = []
    
    if settings.is_development:
        # Load dev site mapping if exists (same as warolabs.com)
        try:
            mapping_path = Path("dev-site-mapping.json")
            if mapping_path.exists():
                dev_site_mapping = json.loads(mapping_path.read_text())
                
                if headers['forwarded_host'] and headers['forwarded_host'] in dev_site_mapping:
                    potential_sites = [dev_site_mapping[headers['forwarded_host']]]
                else:
                    backend_port = headers['host'].split(':')[1] if ':' in headers['host'] else '5001'
                    backend_host = f"localhost:{backend_port}"
                    
                    if backend_host in dev_site_mapping:
                        potential_sites = [dev_site_mapping[backend_host]]
        except:
            pass
    
    if not potential_sites:
        potential_sites = [
            headers['forwarded_host'],
            headers['original_host'],
            headers['origin'].replace('https://', '').replace('http://', '') if headers['origin'] else None,
            headers['referer'].replace('https://', '').replace('http://', '').split('/')[0] if headers['referer'] else None,
            headers['host']
        ]
    
    potential_sites = [site for site in potential_sites if site]
    
    # Check which site exists in tenant_sites (same query as warolabs.com)
    async with get_db_connection() as conn:
        for site in potential_sites:
            result = await conn.fetchrow(
                "SELECT site FROM tenant_sites WHERE site = $1 AND is_active = true",
                site
            )
            if result:
                return site
    
    raise HTTPException(
        status_code=404, 
        detail=f"No tenant found for sites: {', '.join(potential_sites)}"
    )
import asyncio
import os
import sys

# Add the current directory to sys.path
sys.path.append(os.getcwd())

# Set environment variable for localhost mapping to allow port 9999
os.environ["LOCALHOST_MAPPING"] = "localhost:9999=warolabs.com"

from app.database import get_db_connection

async def main():
    print("Checking for session token...")
    # hardcoded valid session from user logs
    session_token = "66019378d6a3f5e6c33031c10740450a" 
    
    try:
        async with get_db_connection() as conn:
            query = """
                SELECT ts.site, ts.tenant_id, ts.brand_name, ts.is_active,
                       t.name as tenant_name, t.slug as tenant_slug
                FROM sessions s
                JOIN tenant_sites ts ON s.tenant_id = ts.tenant_id
                JOIN tenants t ON ts.tenant_id = t.id
                WHERE s.id = $1
            """
            row = await conn.fetchrow(query, session_token)
            if row:
                print(f"Session resolves to tenant: {row['tenant_name']} ({row['tenant_slug']})")
                print(f"Site: {row['site']}")
            else:
                print("Session token not found or invalid")
                
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(main())

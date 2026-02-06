import asyncio
import os
import sys

# Add the current directory to sys.path
sys.path.append(os.getcwd())

from app.database import get_db_connection

async def main():
    site = "warocol.com"
    print(f"Checking for site in tenant_sites: {site}")
    try:
        async with get_db_connection() as conn:
            # Check tenant_sites
            query = "SELECT * FROM tenant_sites WHERE site = $1"
            row = await conn.fetchrow(query, site)
            if row:
                print(f"Found site in tenant_sites: {dict(row)}")
            else:
                print(f"Site '{site}' NOT found in tenant_sites")
                
            # List all sites to see what exists
            rows = await conn.fetch("SELECT site, tenant_id, is_active FROM tenant_sites")
            print("\nExisting sites:")
            for r in rows:
                print(f"- {r['site']} (Tenant: {r['tenant_id']}, Active: {r['is_active']})")
                
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(main())

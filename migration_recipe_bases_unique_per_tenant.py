#!/usr/bin/env python3
"""
Fix unique constraint on product_base_types:
  BEFORE: UNIQUE(name)           — global across all tenants (wrong)
  AFTER:  UNIQUE(name, tenant_id) — unique per tenant (correct)
"""
import asyncio
import asyncpg
from app.config import settings


async def run_migration():
    conn = await asyncpg.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name
    )

    try:
        print("🔧 Fixing unique constraint on product_base_types...")

        # 1. Drop the global unique constraint
        await conn.execute("""
            ALTER TABLE product_base_types
            DROP CONSTRAINT IF EXISTS product_base_types_name_key
        """)
        print("  ✅ Dropped product_base_types_name_key")

        # 2. Add per-tenant unique constraint
        await conn.execute("""
            ALTER TABLE product_base_types
            ADD CONSTRAINT product_base_types_name_tenant_key
            UNIQUE (name, tenant_id)
        """)
        print("  ✅ Added UNIQUE(name, tenant_id)")

        # 3. Verify
        result = await conn.fetchrow("""
            SELECT constraint_name
            FROM information_schema.table_constraints
            WHERE table_name = 'product_base_types'
              AND constraint_name = 'product_base_types_name_tenant_key'
        """)

        if result:
            print("\n✅ Migration completed successfully!")
        else:
            print("\n❌ Constraint not found after migration")

    except Exception as e:
        print(f"❌ Error: {e}")
        raise
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(run_migration())

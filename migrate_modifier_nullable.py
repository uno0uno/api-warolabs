#!/usr/bin/env python3
"""
Migration: Make product_id nullable in modifier_groups table.
This allows modifier groups to be associated with products only via the junction table.
"""
import asyncio
import asyncpg
from app.config import settings

async def run_migration():
    """Make product_id nullable"""

    conn = await asyncpg.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name
    )

    try:
        print("🔧 Making product_id nullable in modifier_groups...")

        await conn.execute("""
            ALTER TABLE modifier_groups
            ALTER COLUMN product_id DROP NOT NULL
        """)

        print("✅ product_id is now nullable")

        # Verify
        result = await conn.fetchrow("""
            SELECT column_name, is_nullable
            FROM information_schema.columns
            WHERE table_name = 'modifier_groups' AND column_name = 'product_id'
        """)

        print(f"   - product_id is_nullable: {result['is_nullable']}")

        print("\n✅ Migration completed successfully!")

    except Exception as e:
        print(f"❌ Error running migration: {e}")
        raise
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(run_migration())

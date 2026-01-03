#!/usr/bin/env python3
"""
Migration: Create product_modifier_groups junction table for N:M relationship
between modifier_groups and products.

This allows a single modifier group to be associated with multiple products.
"""
import asyncio
import asyncpg
from app.config import settings

async def run_migration():
    """Create junction table and migrate existing data"""

    conn = await asyncpg.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name
    )

    try:
        print("🔧 Running migration: Create product_modifier_groups junction table...")

        # Step 1: Create junction table
        print("\n📦 Step 1: Creating junction table...")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS product_modifier_groups (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                product_id UUID NOT NULL REFERENCES product(id) ON DELETE CASCADE,
                modifier_group_id UUID NOT NULL REFERENCES modifier_groups(id) ON DELETE CASCADE,
                tenant_id UUID NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(product_id, modifier_group_id)
            )
        """)
        print("   ✅ Junction table created")

        # Step 2: Create index for better query performance
        print("\n📦 Step 2: Creating indexes...")
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_product_modifier_groups_product_id
            ON product_modifier_groups(product_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_product_modifier_groups_modifier_group_id
            ON product_modifier_groups(modifier_group_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_product_modifier_groups_tenant_id
            ON product_modifier_groups(tenant_id)
        """)
        print("   ✅ Indexes created")

        # Step 3: Migrate existing data from modifier_groups.product_id
        print("\n📦 Step 3: Migrating existing data...")
        migrated = await conn.execute("""
            INSERT INTO product_modifier_groups (product_id, modifier_group_id, tenant_id)
            SELECT product_id, id, tenant_id
            FROM modifier_groups
            WHERE product_id IS NOT NULL
            ON CONFLICT (product_id, modifier_group_id) DO NOTHING
        """)
        print(f"   ✅ Data migrated: {migrated}")

        # Step 4: Verify migration
        print("\n📦 Step 4: Verifying migration...")

        # Count original records
        original_count = await conn.fetchval("""
            SELECT COUNT(*) FROM modifier_groups WHERE product_id IS NOT NULL
        """)

        # Count migrated records
        migrated_count = await conn.fetchval("""
            SELECT COUNT(*) FROM product_modifier_groups
        """)

        print(f"   - Original modifier_groups with product_id: {original_count}")
        print(f"   - Records in junction table: {migrated_count}")

        if original_count == migrated_count:
            print("\n✅ Migration completed successfully!")
        else:
            print(f"\n⚠️  Warning: Count mismatch. Please verify data.")

        # Show table structure
        print("\n📋 Junction table structure:")
        columns = await conn.fetch("""
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_name = 'product_modifier_groups'
            ORDER BY ordinal_position
        """)
        for col in columns:
            print(f"   - {col['column_name']}: {col['data_type']} (nullable: {col['is_nullable']})")

    except Exception as e:
        print(f"❌ Error running migration: {e}")
        raise
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(run_migration())

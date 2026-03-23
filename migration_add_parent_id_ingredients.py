#!/usr/bin/env python3
"""
Migration: add parent_id column to ingredients table for base+variant hierarchy (#66)
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
        print("🔧 Running migration: add parent_id to ingredients...")

        await conn.execute("""
            ALTER TABLE ingredients
            ADD COLUMN IF NOT EXISTS parent_id UUID
            REFERENCES ingredients(id) ON DELETE SET NULL
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ingredients_parent_id
            ON ingredients(parent_id)
        """)

        print("✅ Column and index added successfully")

        col = await conn.fetchrow("""
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_name = 'ingredients' AND column_name = 'parent_id'
        """)

        idx = await conn.fetchrow("""
            SELECT indexname FROM pg_indexes
            WHERE tablename = 'ingredients' AND indexname = 'idx_ingredients_parent_id'
        """)

        print("\n✅ Verification:")
        print(f"  - column: {col['column_name']} ({col['data_type']}, nullable={col['is_nullable']})")
        print(f"  - index:  {idx['indexname']}")

        if col and idx:
            print("\n✅ Migration completed successfully!")
        else:
            print("\n❌ Migration may have failed — column or index not found")

    except Exception as e:
        print(f"❌ Error: {e}")
        raise
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(run_migration())

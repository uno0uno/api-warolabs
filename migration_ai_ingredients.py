#!/usr/bin/env python3
"""
Migration: add ai_generated and ai_generated_at columns to ingredients table
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
        print("🔧 Running migration: add ai_generated columns to ingredients...")

        await conn.execute("""
            ALTER TABLE ingredients
            ADD COLUMN IF NOT EXISTS ai_generated BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS ai_generated_at TIMESTAMP WITH TIME ZONE
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ingredients_ai_generated
            ON ingredients (ai_generated)
            WHERE ai_generated = TRUE
        """)

        print("✅ Columns and index added successfully")

        result = await conn.fetch("""
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = 'ingredients'
            AND column_name IN ('ai_generated', 'ai_generated_at')
            ORDER BY column_name
        """)

        print("\n✅ Verification:")
        for row in result:
            print(f"  - {row['column_name']}: {row['data_type']}")

        if len(result) == 2:
            print("\n✅ Migration completed successfully!")
        else:
            print("\n❌ Migration may have failed - columns not found")

    except Exception as e:
        print(f"❌ Error running migration: {e}")
        raise
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(run_migration())

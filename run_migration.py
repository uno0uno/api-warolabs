#!/usr/bin/env python3
"""
Run database migration to add payment_amount and payment_date columns
"""
import asyncio
import asyncpg
from app.config import settings

async def run_migration():
    """Add missing payment columns to tenant_purchases table"""

    # Create connection
    conn = await asyncpg.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name
    )

    try:
        print("🔧 Running migration: add payment_amount and payment_date columns...")

        # Add columns
        await conn.execute("""
            ALTER TABLE tenant_purchases
            ADD COLUMN IF NOT EXISTS payment_amount NUMERIC,
            ADD COLUMN IF NOT EXISTS payment_date TIMESTAMP WITH TIME ZONE
        """)

        print("✅ Columns added successfully")

        # Verify
        result = await conn.fetch("""
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = 'tenant_purchases'
            AND column_name IN ('payment_amount', 'payment_date')
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

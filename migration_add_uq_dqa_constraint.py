#!/usr/bin/env python3
"""
Migration: add UNIQUE (purchase_item_id, tenant_id) constraint to data_quality_alerts

Required for idempotent ON CONFLICT upserts in run_anomaly_checks_for_purchase()
and get_data_quality(). Without this constraint the ON CONFLICT clause raises a
DB error at runtime.
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
        print("🔧 Running migration: add uq_dqa_item_tenant constraint...")

        await conn.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'uq_dqa_item_tenant'
                ) THEN
                    ALTER TABLE data_quality_alerts
                    ADD CONSTRAINT uq_dqa_item_tenant
                    UNIQUE (purchase_item_id, tenant_id);
                    RAISE NOTICE 'Constraint uq_dqa_item_tenant created.';
                ELSE
                    RAISE NOTICE 'Constraint uq_dqa_item_tenant already exists, skipping.';
                END IF;
            END
            $$;
        """)

        # Verify
        constraint = await conn.fetchrow("""
            SELECT conname, contype
            FROM pg_constraint
            WHERE conname = 'uq_dqa_item_tenant'
        """)

        if constraint:
            print(f"✅ Constraint '{constraint['conname']}' (type={constraint['contype']}) verified in DB")
            print("\n✅ Migration completed successfully!")
        else:
            print("❌ Constraint not found after migration")

    except Exception as e:
        print(f"❌ Error running migration: {e}")
        raise
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(run_migration())

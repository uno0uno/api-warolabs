#!/usr/bin/env python3
"""
Migration: create data_quality_alerts table for purchase anomaly detection

Tracks price spikes, impossible values, and other data entry errors in purchases.
Used by the /api/analytics/data-quality endpoint (issue #48).
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
        print("🔧 Running migration: create data_quality_alerts table...")

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS data_quality_alerts (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                tenant_id UUID NOT NULL REFERENCES tenants(id),
                purchase_item_id UUID REFERENCES tenant_purchase_items(id) ON DELETE SET NULL,
                ingredient_id UUID REFERENCES ingredients(id) ON DELETE SET NULL,
                ingredient_name VARCHAR(255) NOT NULL,
                alert_type VARCHAR(50) NOT NULL,
                severity VARCHAR(20) NOT NULL,
                expected_value NUMERIC,
                actual_value NUMERIC,
                deviation_pct NUMERIC,
                rolling_avg NUMERIC,
                context JSONB,
                resolved BOOLEAN NOT NULL DEFAULT FALSE,
                resolved_by UUID REFERENCES profile(id) ON DELETE SET NULL,
                resolved_at TIMESTAMPTZ,
                resolution_note TEXT,
                original_value NUMERIC,
                corrected_value NUMERIC,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        print("✅ Table created")

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_dqa_tenant_resolved
                ON data_quality_alerts(tenant_id, resolved)
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_dqa_severity
                ON data_quality_alerts(severity, resolved)
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_dqa_purchase_item
                ON data_quality_alerts(purchase_item_id)
                WHERE purchase_item_id IS NOT NULL
        """)
        print("✅ Indexes created")

        # Verify
        columns = await conn.fetch("""
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_name = 'data_quality_alerts'
            ORDER BY ordinal_position
        """)

        print(f"\n✅ Verification — {len(columns)} columns:")
        for col in columns:
            nullable = "NULL" if col['is_nullable'] == 'YES' else "NOT NULL"
            print(f"  - {col['column_name']}: {col['data_type']} {nullable}")

        indexes = await conn.fetch("""
            SELECT indexname FROM pg_indexes
            WHERE tablename = 'data_quality_alerts'
            ORDER BY indexname
        """)
        print(f"\n✅ Indexes ({len(indexes)}):")
        for idx in indexes:
            print(f"  - {idx['indexname']}")

        if len(columns) == 19 and len(indexes) == 4:
            print("\n✅ Migration completed successfully!")
        else:
            print(f"\n⚠️  Expected 19 columns and 4 indexes, got {len(columns)} columns and {len(indexes)} indexes")

    except Exception as e:
        print(f"❌ Error running migration: {e}")
        raise
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(run_migration())

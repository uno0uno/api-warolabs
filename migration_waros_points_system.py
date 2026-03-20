#!/usr/bin/env python3
"""
Migration: waro_earning_rules + waro_manual_assignments

Creates the two new tables needed for the configurable Waros points system (issue #52).
No existing tables are modified. Zero impact on existing data.

Tables created:
  - waro_earning_rules: tenant-configurable earning rules (JSONB config per rule_type)
  - waro_manual_assignments: audit log for manual point assignments by admins

Existing tables reused (no changes):
  - waros_wallets, waros_transactions, gamification_config
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
        print("🔧 Running migration: waro_earning_rules + waro_manual_assignments...")

        # ── Table 1: waro_earning_rules ──────────────────────────────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS waro_earning_rules (
                id          SERIAL PRIMARY KEY,
                tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                rule_type   VARCHAR(30) NOT NULL,
                rule_name   VARCHAR(255) NOT NULL,
                is_active   BOOLEAN NOT NULL DEFAULT false,
                config      JSONB NOT NULL DEFAULT '{}',
                created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT waro_earning_rules_tenant_type_unique UNIQUE (tenant_id, rule_type),
                CONSTRAINT waro_earning_rules_valid_type CHECK (
                    rule_type IN ('ticket_value', 'purchase_count', 'frequency', 'per_ticket_qty')
                )
            )
        """)
        print("  ✅ waro_earning_rules created")

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_waro_earning_rules_tenant
                ON waro_earning_rules(tenant_id, is_active)
        """)
        print("  ✅ waro_earning_rules index created")

        # ── Table 2: waro_manual_assignments ────────────────────────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS waro_manual_assignments (
                id            SERIAL PRIMARY KEY,
                tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                profile_id    UUID NOT NULL REFERENCES profile(id) ON DELETE CASCADE,
                waros_amount  INTEGER NOT NULL,
                reason        TEXT,
                assigned_by   UUID NOT NULL REFERENCES profile(id) ON DELETE RESTRICT,
                created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT waro_manual_assignments_nonzero CHECK (waros_amount != 0)
            )
        """)
        print("  ✅ waro_manual_assignments created")

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_waro_manual_assignments_tenant_date
                ON waro_manual_assignments(tenant_id, created_at DESC)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_waro_manual_assignments_profile
                ON waro_manual_assignments(profile_id)
        """)
        print("  ✅ waro_manual_assignments indexes created")

        # ── Verification ─────────────────────────────────────────────────────
        print("\n🔍 Verification:")

        for table, expected_cols, expected_idx in [
            ("waro_earning_rules", 8, 3),  # PK + UNIQUE + explicit index
            ("waro_manual_assignments", 7, 3),
        ]:
            columns = await conn.fetch("""
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_name = $1
                ORDER BY ordinal_position
            """, table)

            indexes = await conn.fetch("""
                SELECT indexname FROM pg_indexes
                WHERE tablename = $1
                ORDER BY indexname
            """, table)

            print(f"\n  {table} — {len(columns)} cols, {len(indexes)} indexes:")
            for col in columns:
                nullable = "NULL" if col['is_nullable'] == 'YES' else "NOT NULL"
                print(f"    - {col['column_name']}: {col['data_type']} {nullable}")
            for idx in indexes:
                print(f"    - index: {idx['indexname']}")

            if len(columns) == expected_cols and len(indexes) == expected_idx:
                print(f"  ✅ {table} OK")
            else:
                print(f"  ⚠️  Expected {expected_cols} cols / {expected_idx} indexes")

        # Confirm no existing tables touched
        existing = await conn.fetch("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name IN ('waros_wallets','waros_transactions','gamification_config')
        """)
        print(f"\n  ✅ Existing waros tables untouched: {[r['table_name'] for r in existing]}")

        print("\n✅ Migration completed successfully!")

    except Exception as e:
        print(f"❌ Error running migration: {e}")
        raise
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(run_migration())

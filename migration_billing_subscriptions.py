#!/usr/bin/env python3
"""
Migration: billing system tables (issue #59)

Creates the 4 tables needed for the subscription/billing system.
No existing tables are modified. Zero impact on existing data.

Tables created:
  - subscription_plans: plan catalog (Free, Pro, etc.)
  - tenant_subscriptions: one active plan per tenant (UNIQUE tenant_id)
  - scan_usage: monthly scan counter per tenant/period
  - billing_events: audit log of payment and lifecycle events

Seed data inserted:
  - subscription_plans: 'free' and 'pro' plans
  - modules: 'compras-directas-ia'
  - tools: 'ia-scanner'
  - module_tools: link between the module and tool above
  - tenant_subscriptions: all existing tenants assigned to 'free' plan

Existing tables referenced (no changes):
  - tenants (FK source)
  - modules, tools, module_tools (seed targets, already exist)
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
        print("🔧 Running migration: billing system tables...")

        # ── Table 1: subscription_plans ──────────────────────────────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS subscription_plans (
                id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                name          VARCHAR(100) NOT NULL,
                slug          VARCHAR(100) UNIQUE NOT NULL,
                description   TEXT,
                price_monthly NUMERIC(10,2) NOT NULL DEFAULT 0,
                price_annual  NUMERIC(10,2) NOT NULL DEFAULT 0,
                scan_limit    INTEGER NOT NULL DEFAULT 1000,
                is_active     BOOLEAN DEFAULT TRUE,
                features      JSONB DEFAULT '{}',
                created_at    TIMESTAMPTZ DEFAULT now(),
                updated_at    TIMESTAMPTZ DEFAULT now()
            )
        """)
        print("  ✅ subscription_plans created")

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_subscription_plans_slug
                ON subscription_plans(slug)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_subscription_plans_active
                ON subscription_plans(is_active)
        """)
        print("  ✅ subscription_plans indexes created")

        # ── Table 2: tenant_subscriptions ────────────────────────────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tenant_subscriptions (
                id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                tenant_id            UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                plan_id              UUID NOT NULL REFERENCES subscription_plans(id),
                billing_cycle        VARCHAR(10) NOT NULL
                                       CHECK (billing_cycle IN ('monthly', 'annual')),
                status               VARCHAR(20) DEFAULT 'pending'
                                       CHECK (status IN (
                                           'pending', 'active', 'past_due', 'cancelled', 'expired'
                                       )),
                current_period_start TIMESTAMPTZ NOT NULL,
                current_period_end   TIMESTAMPTZ NOT NULL,
                mp_subscription_id   TEXT,
                mp_preapproval_id    TEXT,
                cancelled_at         TIMESTAMPTZ,
                created_at           TIMESTAMPTZ DEFAULT now(),
                updated_at           TIMESTAMPTZ DEFAULT now(),
                UNIQUE (tenant_id)
            )
        """)
        print("  ✅ tenant_subscriptions created")

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_tenant_subscriptions_tenant
                ON tenant_subscriptions(tenant_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_tenant_subscriptions_status
                ON tenant_subscriptions(status, plan_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_tenant_subscriptions_period
                ON tenant_subscriptions(current_period_end)
        """)
        print("  ✅ tenant_subscriptions indexes created")

        # ── Table 3: scan_usage ───────────────────────────────────────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS scan_usage (
                id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                subscription_id UUID REFERENCES tenant_subscriptions(id) ON DELETE SET NULL,
                period_start    TIMESTAMPTZ NOT NULL,
                period_end      TIMESTAMPTZ NOT NULL,
                scans_used      INTEGER NOT NULL DEFAULT 0,
                scans_limit     INTEGER NOT NULL DEFAULT 1000,
                last_scanned_at TIMESTAMPTZ,
                created_at      TIMESTAMPTZ DEFAULT now(),
                updated_at      TIMESTAMPTZ DEFAULT now(),
                UNIQUE (tenant_id, period_start)
            )
        """)
        print("  ✅ scan_usage created")

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_scan_usage_tenant_period
                ON scan_usage(tenant_id, period_start DESC)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_scan_usage_subscription
                ON scan_usage(subscription_id)
        """)
        print("  ✅ scan_usage indexes created")

        # ── Table 4: billing_events ───────────────────────────────────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS billing_events (
                id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                subscription_id UUID REFERENCES tenant_subscriptions(id) ON DELETE SET NULL,
                event_type      VARCHAR(50) NOT NULL,
                amount          NUMERIC(10,2),
                currency        VARCHAR(3) DEFAULT 'COP',
                mp_payment_id   TEXT,
                metadata        JSONB DEFAULT '{}',
                created_at      TIMESTAMPTZ DEFAULT now()
            )
        """)
        print("  ✅ billing_events created")

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_billing_events_tenant
                ON billing_events(tenant_id, created_at DESC)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_billing_events_subscription
                ON billing_events(subscription_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_billing_events_event_type
                ON billing_events(event_type)
        """)
        print("  ✅ billing_events indexes created")

        # ── Seed 1: subscription plans ────────────────────────────────────────
        print("\n🌱 Seeding subscription plans...")

        free_row = await conn.fetchrow("""
            INSERT INTO subscription_plans (name, slug, price_monthly, price_annual, scan_limit)
            VALUES ('Free', 'free', 0, 0, 1000)
            ON CONFLICT (slug) DO NOTHING
            RETURNING id
        """)

        # ON CONFLICT DO NOTHING returns nothing if the row already existed
        if free_row is None:
            free_row = await conn.fetchrow(
                "SELECT id FROM subscription_plans WHERE slug = 'free'"
            )
            print("  ⚠️  plan 'free' already existed — using existing id")
        else:
            print("  ✅ plan 'free' inserted")

        free_plan_id = free_row["id"]

        pro_row = await conn.fetchrow("""
            INSERT INTO subscription_plans (
                name, slug, price_monthly, price_annual, scan_limit, description
            )
            VALUES (
                'Pro', 'pro', 49900, 479000, 5000,
                'Plan profesional con mayor límite de escaneos'
            )
            ON CONFLICT (slug) DO NOTHING
            RETURNING id
        """)

        if pro_row is None:
            print("  ⚠️  plan 'pro' already existed — skipped")
        else:
            print("  ✅ plan 'pro' inserted")

        # ── Seed 2: module + tool + module_tools link ─────────────────────────
        print("\n🌱 Seeding module, tool, and link...")

        module_row = await conn.fetchrow("""
            INSERT INTO modules (name, slug, description)
            VALUES (
                'Compras Directas IA',
                'compras-directas-ia',
                'Escaneo inteligente de facturas de proveedores'
            )
            ON CONFLICT (slug) DO NOTHING
            RETURNING id
        """)

        if module_row is None:
            module_row = await conn.fetchrow(
                "SELECT id FROM modules WHERE slug = 'compras-directas-ia'"
            )
            print("  ⚠️  module 'compras-directas-ia' already existed — using existing id")
        else:
            print("  ✅ module 'compras-directas-ia' inserted")

        tool_row = await conn.fetchrow("""
            INSERT INTO tools (name, slug, description)
            VALUES (
                'Escaner IA',
                'ia-scanner',
                'Escaneo de documentos con IA (límite por plan)'
            )
            ON CONFLICT (slug) DO NOTHING
            RETURNING id
        """)

        if tool_row is None:
            tool_row = await conn.fetchrow(
                "SELECT id FROM tools WHERE slug = 'ia-scanner'"
            )
            print("  ⚠️  tool 'ia-scanner' already existed — using existing id")
        else:
            print("  ✅ tool 'ia-scanner' inserted")

        # Link module ↔ tool via junction table (discovered in research — not in original issue)
        await conn.execute("""
            INSERT INTO module_tools (module_id, tool_id)
            VALUES ($1, $2)
            ON CONFLICT (module_id, tool_id) DO NOTHING
        """, module_row["id"], tool_row["id"])
        print("  ✅ module_tools link inserted (compras-directas-ia ↔ ia-scanner)")

        # ── Seed 3: assign free plan to all existing tenants ──────────────────
        print("\n🌱 Assigning free plan to existing tenants...")

        inserted = await conn.execute("""
            INSERT INTO tenant_subscriptions
                (tenant_id, plan_id, billing_cycle, status,
                 current_period_start, current_period_end)
            SELECT
                t.id,
                $1,
                'monthly',
                'active',
                date_trunc('month', now()),
                date_trunc('month', now()) + interval '1 month'
            FROM tenants t
            ON CONFLICT (tenant_id) DO NOTHING
        """, free_plan_id)

        # asyncpg returns "INSERT 0 N" as a string
        rows_inserted = int(inserted.split()[-1])
        print(f"  ✅ {rows_inserted} tenant subscription(s) inserted")

        # ── Verification ──────────────────────────────────────────────────────
        print("\n🔍 Verification:")

        for table, expected_cols in [
            ("subscription_plans", 11),
            ("tenant_subscriptions", 12),
            ("scan_usage", 10),
            ("billing_events", 9),
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
                nullable = "NULL" if col["is_nullable"] == "YES" else "NOT NULL"
                print(f"    - {col['column_name']}: {col['data_type']} {nullable}")
            for idx in indexes:
                print(f"    - index: {idx['indexname']}")

            status = "✅" if len(columns) == expected_cols else "⚠️ "
            print(f"  {status} {table}: {len(columns)} cols (expected {expected_cols})")

        # Seed counts
        counts = await conn.fetchrow("""
            SELECT
                (SELECT COUNT(*) FROM subscription_plans)     AS plans,
                (SELECT COUNT(*) FROM tenant_subscriptions)   AS subscriptions,
                (SELECT COUNT(*) FROM modules
                 WHERE slug = 'compras-directas-ia')          AS modules,
                (SELECT COUNT(*) FROM tools
                 WHERE slug = 'ia-scanner')                   AS tools,
                (SELECT COUNT(*) FROM module_tools)           AS module_tool_links
        """)

        print(f"""
  📊 Seed counts:
    - subscription_plans:   {counts['plans']}
    - tenant_subscriptions: {counts['subscriptions']}
    - modules (ia):         {counts['modules']}
    - tools (ia):           {counts['tools']}
    - module_tools links:   {counts['module_tool_links']}""")

        print("\n✅ Migration completed successfully!")

    except Exception as e:
        print(f"❌ Error running migration: {e}")
        raise
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(run_migration())

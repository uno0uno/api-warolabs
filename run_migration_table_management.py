#!/usr/bin/env python3
"""
Migration #297 — Table Management: create tables, table_sessions, add orders.table_session_id

Creates:
  - TABLE tables            (physical restaurant tables per tenant)
  - TABLE table_sessions    (one session per table visit)
  - COLUMN orders.table_session_id  (nullable FK — existing orders unaffected)

Indexes added:
  - idx_tables_tenant             ON tables(tenant_id)
  - idx_table_sessions_table      ON table_sessions(table_id)
  - idx_table_sessions_open       ON table_sessions(table_id) WHERE closed_at IS NULL
  - idx_orders_table_session      ON orders(table_session_id)

FK fixes vs issue SQL:
  - table_sessions.tenant_id         → REFERENCES tenants(id)
  - table_sessions.opened_by_user_id → REFERENCES profile(id) ON DELETE SET NULL

Epic: https://github.com/uno0uno/warocol.com/issues/292
Issue: https://github.com/uno0uno/warocol.com/issues/297
"""
import asyncio
import asyncpg
from app.config import settings


async def run() -> None:
    conn = await asyncpg.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name,
    )
    try:
        # ── Pre-flight checks ─────────────────────────────────────────────────
        print("🔍 Running pre-flight checks...")

        tables_exists = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'tables')"
        )
        if tables_exists:
            raise Exception("Pre-flight failed: table 'tables' already exists — aborting.")

        sessions_exists = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'table_sessions')"
        )
        if sessions_exists:
            raise Exception("Pre-flight failed: table 'table_sessions' already exists — aborting.")

        col_exists = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'orders' AND column_name = 'table_session_id')"
        )
        if col_exists:
            raise Exception("Pre-flight failed: column orders.table_session_id already exists — aborting.")

        print("   ✅ Pre-flight passed — safe to proceed")

        # ── DDL (single transaction) ──────────────────────────────────────────
        async with conn.transaction():

            # 1. tables
            print("🔧 Step 1: Creating table 'tables'...")
            await conn.execute("""
                CREATE TABLE tables (
                    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    tenant_id  UUID NOT NULL REFERENCES tenants(id),
                    name       VARCHAR(50) NOT NULL,
                    capacity   INT,
                    status     VARCHAR(20) NOT NULL DEFAULT 'free',
                    is_active  BOOL NOT NULL DEFAULT true,
                    created_at TIMESTAMPTZ DEFAULT now()
                )
            """)
            await conn.execute(
                "CREATE INDEX idx_tables_tenant ON tables(tenant_id)"
            )
            print("   ✅ Table 'tables' created")

            # 2. table_sessions
            print("🔧 Step 2: Creating table 'table_sessions'...")
            await conn.execute("""
                CREATE TABLE table_sessions (
                    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    table_id           UUID NOT NULL REFERENCES tables(id),
                    tenant_id          UUID NOT NULL REFERENCES tenants(id),
                    opened_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
                    closed_at          TIMESTAMPTZ,
                    opened_by_user_id  UUID REFERENCES profile(id) ON DELETE SET NULL
                )
            """)
            await conn.execute(
                "CREATE INDEX idx_table_sessions_table ON table_sessions(table_id)"
            )
            # Partial index — optimises "active tables" dashboard (most common query)
            await conn.execute(
                "CREATE INDEX idx_table_sessions_open "
                "ON table_sessions(table_id) WHERE closed_at IS NULL"
            )
            print("   ✅ Table 'table_sessions' created")

            # 3. orders.table_session_id
            print("🔧 Step 3: Adding orders.table_session_id...")
            await conn.execute(
                "ALTER TABLE orders "
                "ADD COLUMN table_session_id UUID REFERENCES table_sessions(id)"
            )
            await conn.execute(
                "CREATE INDEX idx_orders_table_session ON orders(table_session_id)"
            )
            print("   ✅ Column orders.table_session_id added")

        print("\n✅ Migration #297 completed successfully")

        # ── Post-migration verification ───────────────────────────────────────
        print("\n🔍 Post-migration verification...")
        ok = True

        for tbl in ("tables", "table_sessions"):
            exists = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = $1)",
                tbl,
            )
            if exists:
                print(f"   ✅ Table '{tbl}' exists")
            else:
                print(f"   ❌ Table '{tbl}' NOT found — unexpected")
                ok = False

        col_ok = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'orders' AND column_name = 'table_session_id')"
        )
        if col_ok:
            print("   ✅ Column orders.table_session_id exists")
        else:
            print("   ❌ Column orders.table_session_id NOT found — unexpected")
            ok = False

        for idx in (
            "idx_tables_tenant",
            "idx_table_sessions_table",
            "idx_table_sessions_open",
            "idx_orders_table_session",
        ):
            idx_ok = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_indexes "
                "WHERE schemaname = 'public' AND indexname = $1)",
                idx,
            )
            if idx_ok:
                print(f"   ✅ Index '{idx}' exists")
            else:
                print(f"   ❌ Index '{idx}' NOT found — unexpected")
                ok = False

        if ok:
            print("\n✅ All verifications passed")
        else:
            raise Exception("Post-migration verification failed — check output above")

    except Exception as e:
        print(f"\n❌ Migration failed: {e}")
        raise
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(run())

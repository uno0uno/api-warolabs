#!/usr/bin/env python3
"""
Migration #302 — Add tables_enabled flag to tenant_public_profiles

Adds:
  - COLUMN tenant_public_profiles.tables_enabled  BOOL NOT NULL DEFAULT false

All existing rows get tables_enabled = false (no tenant is affected by the
table management epic rollout until they explicitly opt in via the toggle).

Issue: https://github.com/uno0uno/warocol.com/issues/302
Epic:  https://github.com/uno0uno/warocol.com/issues/292
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
        # ── Pre-flight ────────────────────────────────────────────────────────
        print("🔍 Pre-flight check...")
        col_exists = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'tenant_public_profiles' AND column_name = 'tables_enabled')"
        )
        if col_exists:
            raise Exception(
                "Pre-flight failed: column 'tables_enabled' already exists — aborting."
            )
        print("   ✅ Pre-flight passed")

        # ── DDL ───────────────────────────────────────────────────────────────
        print("🔧 Adding column tables_enabled...")
        async with conn.transaction():
            await conn.execute("""
                ALTER TABLE tenant_public_profiles
                ADD COLUMN tables_enabled BOOL NOT NULL DEFAULT false
            """)
        print("   ✅ Column added")

        # ── Post-migration verification ───────────────────────────────────────
        print("🔍 Post-migration verification...")
        ok = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'tenant_public_profiles' AND column_name = 'tables_enabled')"
        )
        if ok:
            print("   ✅ Column 'tables_enabled' exists")
        else:
            raise Exception("Post-migration failed: column not found after migration")

        count = await conn.fetchval(
            "SELECT COUNT(*) FROM tenant_public_profiles WHERE tables_enabled = false"
        )
        print(f"   ✅ {count} existing profiles set to tables_enabled=false")

        print("\n✅ Migration #302 completed successfully")

    except Exception as e:
        print(f"\n❌ Migration failed: {e}")
        raise
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(run())

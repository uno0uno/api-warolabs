#!/usr/bin/env python3
"""
Migration #102 — Fix UNIQUE(name) constraint on ingredients to support tenant-scoped names.

Replaces the global UNIQUE(name) constraint with a composite scoped index:
  UNIQUE(name, COALESCE(tenant_id, '00000000-0000-0000-0000-000000000000'))

This allows:
  - Global ingredients: unique by name among globals (tenant_id IS NULL)
  - Tenant ingredients: unique by name within that tenant
  - Two tenants CAN have an ingredient with the same name
  - A tenant CAN have an ingredient with the same name as a global

Also cleans up a duplicate idx_ingredients_tenant index in the same transaction.

Epic: https://github.com/uno0uno/warocol.com/issues/289
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
        print("🔍 Running pre-flight conflict check...")
        conflicts = await conn.fetch(
            """
            SELECT name, COALESCE(tenant_id::text, 'global') AS scope, COUNT(*) AS cnt
            FROM ingredients
            GROUP BY name, COALESCE(tenant_id::text, 'global')
            HAVING COUNT(*) > 1
            """
        )
        if conflicts:
            raise Exception(
                f"Pre-flight failed: {len(conflicts)} name conflicts found — aborting.\n"
                + "\n".join(f"  name={r['name']} scope={r['scope']} count={r['cnt']}" for r in conflicts)
            )
        print("   ✅ No conflicts found — safe to proceed")

        async with conn.transaction():
            # Step 1: drop global UNIQUE constraint
            print("🔧 Step 1: Dropping ingredients_name_key (global UNIQUE)...")
            await conn.execute(
                "ALTER TABLE ingredients DROP CONSTRAINT ingredients_name_key"
            )
            print("   ✅ Constraint dropped")

            # Step 2: create scoped UNIQUE index
            # COALESCE maps NULL → fixed sentinel UUID so all globals are treated as one group.
            # PostgreSQL btree indexes treat NULL != NULL, making COALESCE necessary.
            print("🔧 Step 2: Creating ingredients_name_tenant_unique (scoped UNIQUE)...")
            await conn.execute(
                """
                CREATE UNIQUE INDEX ingredients_name_tenant_unique
                ON ingredients (
                    name,
                    COALESCE(tenant_id, '00000000-0000-0000-0000-000000000000'::uuid)
                )
                """
            )
            print("   ✅ Scoped unique index created")

            # Step 3: drop duplicate tenant_id index
            # idx_ingredients_tenant and idx_ingredients_tenant_id are identical btree(tenant_id)
            print("🔧 Step 3: Dropping duplicate idx_ingredients_tenant index...")
            await conn.execute("DROP INDEX IF EXISTS idx_ingredients_tenant")
            print("   ✅ Duplicate index removed")

        print("\n✅ Migration #102 completed successfully")
        print("   - Dropped:  ingredients_name_key (global UNIQUE)")
        print("   - Created:  ingredients_name_tenant_unique (scoped UNIQUE per tenant)")
        print("   - Dropped:  idx_ingredients_tenant (duplicate of idx_ingredients_tenant_id)")

        # Post-migration verification
        print("\n🔍 Post-migration verification...")
        old_constraint = await conn.fetchrow(
            """
            SELECT conname FROM pg_constraint
            WHERE conrelid = 'ingredients'::regclass AND conname = 'ingredients_name_key'
            """
        )
        new_index = await conn.fetchrow(
            """
            SELECT indexname FROM pg_indexes
            WHERE tablename = 'ingredients' AND indexname = 'ingredients_name_tenant_unique'
            """
        )
        dup_index = await conn.fetchrow(
            """
            SELECT indexname FROM pg_indexes
            WHERE tablename = 'ingredients' AND indexname = 'idx_ingredients_tenant'
            """
        )

        ok = True
        if old_constraint:
            print("   ❌ ingredients_name_key still exists — unexpected")
            ok = False
        else:
            print("   ✅ ingredients_name_key removed")

        if new_index:
            print("   ✅ ingredients_name_tenant_unique present")
        else:
            print("   ❌ ingredients_name_tenant_unique not found — unexpected")
            ok = False

        if dup_index:
            print("   ❌ idx_ingredients_tenant still exists — unexpected")
            ok = False
        else:
            print("   ✅ idx_ingredients_tenant removed")

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

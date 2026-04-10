#!/usr/bin/env python3
"""
DB Migration: Payment Method Groups (Issue #330)

Adds:
  - payment_method_groups table
      tenant_id = NULL  → global default (cash, card, digital, credit)
      tenant_id = UUID  → tenant-created custom group
  - payment_methods table (subtypes: Visa, Mastercard, Nequi…)
  - orders.payment_method_id  UUID NULL FK → payment_methods (backward compat)
  - closing_summary.payment_breakdown  JSONB NULL

Safe to run multiple times (all statements use IF NOT EXISTS / ADD COLUMN IF NOT EXISTS).
Seeds 4 global default groups once (ON CONFLICT DO NOTHING).
"""
import asyncio
import os
import asyncpg
from app.config import settings


async def run_migration() -> None:
    confirm = input(
        "\n¿Ejecutar migración de Payment Method Groups en la base de datos actual"
        f" ({settings.db_name} @ {settings.db_host})? [y/N] "
    )
    if confirm.strip().lower() != "y":
        print("Migración cancelada.")
        return

    conn = await asyncpg.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name,
    )

    try:
        print("\nRunning Payment Method Groups migration...")

        # ── Step 1: Execute SQL migration file ────────────────────────────────
        sql_path = os.path.join(
            os.path.dirname(__file__), "migrations", "005_payment_method_groups.sql"
        )
        with open(sql_path, "r") as f:
            sql = f.read()

        await conn.execute(sql)
        print("  [ok] Tables and columns created (payment_method_groups, payment_methods)")
        print("  [ok] orders.payment_method_id column added")
        print("  [ok] closing_summary.payment_breakdown column added")

        # ── Step 2: Seed 4 global default groups (idempotent) ─────────────────
        # These 4 slugs match the exact values in orders.payment_method in production.
        # tenant_id = NULL means visible to ALL tenants, including future ones.
        await conn.execute("""
            INSERT INTO payment_method_groups
                (tenant_id, slug, name, triggers_cartera, sort_order)
            VALUES
                (NULL, 'cash',    'Efectivo', false, 0),
                (NULL, 'card',    'Tarjeta',  false, 1),
                (NULL, 'digital', 'Digital',  false, 2),
                (NULL, 'credit',  'Crédito',  true,  3)
            ON CONFLICT (slug) WHERE tenant_id IS NULL DO NOTHING
        """)
        print("  [ok] Global default groups seeded (cash, card, digital, credit)")

        # ── Step 3: Verification ───────────────────────────────────────────────
        groups = await conn.fetch("""
            SELECT slug, name, triggers_cartera, sort_order
            FROM payment_method_groups
            WHERE tenant_id IS NULL
            ORDER BY sort_order
        """)
        print("\nVerification — global default groups:")
        for row in groups:
            cartera_tag = " [triggers_cartera=TRUE]" if row["triggers_cartera"] else ""
            print(f"  - {row['slug']}: {row['name']}{cartera_tag}")

        order_col = await conn.fetchval("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'orders' AND column_name = 'payment_method_id'
        """)
        print(f"\nVerification — orders.payment_method_id exists: {bool(order_col)}")

        summary_col = await conn.fetchval("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'closing_summary' AND column_name = 'payment_breakdown'
        """)
        print(f"Verification — closing_summary.payment_breakdown exists: {bool(summary_col)}")

        existing_orders = await conn.fetchval(
            "SELECT COUNT(*) FROM orders WHERE payment_method IS NOT NULL"
        )
        print(f"\nExisting orders with payment_method (unchanged): {existing_orders}")

        if len(groups) == 4 and order_col and summary_col:
            print("\n✅ Migration completed successfully!")
        else:
            print("\n⚠️  WARNING: Some objects may not have been created — check output above.")

    except Exception as exc:
        print(f"\nERROR: {exc}")
        raise
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(run_migration())

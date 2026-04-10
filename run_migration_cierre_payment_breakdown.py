#!/usr/bin/env python3
"""
DB Migration: Cierre Payment Breakdown (Issue #336)

Creates:
  - cierre_payment_breakdown table
      cierre_id → FK to closing_summary(id) ON DELETE CASCADE
      group_slug, method_name, total per row
  - Index on cierre_id for fast lookup

Note: closing_summary.payment_breakdown JSONB column (from migration 005)
stays in schema but is never populated — superseded by this table.

Safe to run multiple times (CREATE TABLE IF NOT EXISTS, CREATE INDEX IF NOT EXISTS).
"""
import asyncio
import os
import asyncpg
from app.config import settings


async def run_migration() -> None:
    confirm = input(
        "\n¿Ejecutar migración de Cierre Payment Breakdown en la base de datos actual"
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
        print("\nRunning Cierre Payment Breakdown migration...")

        # ── Execute SQL migration file ─────────────────────────────────────────
        sql_path = os.path.join(
            os.path.dirname(__file__), "migrations", "006_cierre_payment_breakdown.sql"
        )
        with open(sql_path, "r") as f:
            sql = f.read()

        await conn.execute(sql)
        print("  [ok] Table cierre_payment_breakdown created")
        print("  [ok] Index cierre_payment_breakdown_cierre_id_idx created")

        # ── Verification ───────────────────────────────────────────────────────
        table_exists = await conn.fetchval(
            "SELECT to_regclass('public.cierre_payment_breakdown')"
        )
        index_exists = await conn.fetchval("""
            SELECT indexname FROM pg_indexes
            WHERE tablename = 'cierre_payment_breakdown'
              AND indexname = 'cierre_payment_breakdown_cierre_id_idx'
        """)
        closing_count = await conn.fetchval("SELECT COUNT(*) FROM closing_summary")
        breakdown_count = await conn.fetchval("SELECT COUNT(*) FROM cierre_payment_breakdown")

        print("\nVerification:")
        print(f"  cierre_payment_breakdown table exists: {bool(table_exists)}")
        print(f"  cierre_payment_breakdown_cierre_id_idx index exists: {bool(index_exists)}")
        print(f"  closing_summary rows (unchanged): {closing_count}")
        print(f"  cierre_payment_breakdown rows (new, should be 0): {breakdown_count}")

        if table_exists and index_exists:
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

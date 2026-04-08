#!/usr/bin/env python3
"""
DB Migration: Credit Sales (Issue #294)

Adds:
  - orders.payment_status   VARCHAR(20) NULLABLE
      NULL    = order still open / not yet checked out (mesa pending)
      'paid'  = completed with cash / card / digital
      'credit'= completed with credit (awaiting payment)
      'partial'= credit with partial payment registered
  - orders.credit_due_date  DATE NULLABLE
  - orders.credit_paid_amount NUMERIC(12,2) NOT NULL DEFAULT 0

  - credit_payments table (mirrors purchase_payments pattern)
  - Partial index on orders(tenant_id, payment_status) for Cartera queries

Safe to run multiple times (all statements use IF NOT EXISTS / ADD COLUMN IF NOT EXISTS).
Coordinates safely with #303: both use ADD COLUMN IF NOT EXISTS.
"""
import asyncio
import asyncpg
from app.config import settings


async def run_migration() -> None:
    confirm = input(
        "\n¿Ejecutar migración de Credit Sales en la base de datos actual"
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
        print("\nRunning Credit Sales migration...")

        # ── Step 1: new columns on orders ────────────────────────────────────
        # payment_status is NULLABLE — NULL means order still open/not checked out
        await conn.execute("""
            ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS payment_status VARCHAR(20)
        """)
        print("  [ok] orders.payment_status column")

        await conn.execute("""
            ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS credit_due_date DATE
        """)
        print("  [ok] orders.credit_due_date column")

        # credit_paid_amount is a running counter — safe to default to 0 for all rows
        await conn.execute("""
            ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS credit_paid_amount NUMERIC(12,2) NOT NULL DEFAULT 0
        """)
        print("  [ok] orders.credit_paid_amount column")

        # ── Step 2: partial index for Cartera queries ─────────────────────────
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_orders_payment_status
            ON orders(tenant_id, payment_status)
            WHERE payment_status IN ('credit', 'partial')
        """)
        print("  [ok] idx_orders_payment_status index")

        # ── Step 3: credit_payments table ─────────────────────────────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS credit_payments (
                id                 UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
                order_id           UUID          NOT NULL REFERENCES orders(id),
                customer_id        UUID          NOT NULL,
                tenant_id          UUID          NOT NULL REFERENCES tenants(id),
                amount             NUMERIC(12,2) NOT NULL CHECK (amount > 0),
                payment_date       TIMESTAMPTZ   NOT NULL DEFAULT now(),
                payment_method     VARCHAR(20)   NOT NULL,
                notes              TEXT,
                created_by_user_id UUID,
                created_at         TIMESTAMPTZ   NOT NULL DEFAULT now()
            )
        """)
        print("  [ok] credit_payments table")

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_credit_payments_order
            ON credit_payments(order_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_credit_payments_tenant
            ON credit_payments(tenant_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_credit_payments_customer
            ON credit_payments(customer_id, tenant_id)
        """)
        print("  [ok] credit_payments indexes")

        # ── Step 4: Verification ──────────────────────────────────────────────
        order_cols = await conn.fetch("""
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_name = 'orders'
              AND column_name IN ('payment_status', 'credit_due_date', 'credit_paid_amount')
            ORDER BY column_name
        """)
        print("\nVerification — orders columns:")
        for row in order_cols:
            print(f"  - {row['column_name']}: {row['data_type']} (nullable={row['is_nullable']})")

        table_exists = await conn.fetchval("""
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_name = 'credit_payments'
        """)
        print(f"\nVerification — credit_payments table exists: {bool(table_exists)}")

        if len(order_cols) == 3 and table_exists:
            print("\nMigration completed successfully!")
        else:
            print("\nWARNING: Some objects may not have been created — check output above.")

    except Exception as exc:
        print(f"\nERROR: {exc}")
        raise
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(run_migration())


import asyncio
import asyncpg
from app.config import settings

async def verify_and_fix_db():
    conn = await asyncpg.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name
    )

    try:
        print("🔍 Verifying schema for salary feature...")

        # 1. Check employee_salaries columns
        print("Checking employee_salaries table...")
        columns = await conn.fetch("""
            SELECT column_name, data_type 
            FROM information_schema.columns 
            WHERE table_name = 'employee_salaries'
        """)
        col_names = [c['column_name'] for c in columns]
        
        # Add missing columns
        if 'payment_frequency' not in col_names:
            print("  ➕ Adding missing column: payment_frequency")
            await conn.execute("ALTER TABLE employee_salaries ADD COLUMN IF NOT EXISTS payment_frequency VARCHAR(20) DEFAULT 'monthly'")
        
        if 'salary_type' not in col_names:
            print("  ➕ Adding missing column: salary_type")
            await conn.execute("ALTER TABLE employee_salaries ADD COLUMN IF NOT EXISTS salary_type VARCHAR(20) DEFAULT 'smmlv'")
            
            print("  ➕ Adding missing column: fixed_amount")
            await conn.execute("ALTER TABLE employee_salaries ADD COLUMN IF NOT EXISTS fixed_amount NUMERIC(12,2)")

        # Unique Constraint for ON CONFLICT
        print("\nChecking constraints...")
        # Check if index exists
        index_exists = await conn.fetchval("""
            SELECT EXISTS (
                SELECT 1
                FROM pg_indexes
                WHERE tablename = 'employee_salaries'
                AND indexdef LIKE '%UNIQUE INDEX%'
                AND indexdef LIKE '%tenant_member_id%'
                AND indexdef LIKE '%period_month%'
            )
        """)
        
        if not index_exists:
            print("  ➕ Adding missing UNIQUE INDEX for upsert support")
            await conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_employee_salaries_unique 
                ON employee_salaries(tenant_member_id, period_month)
            """)
            print("  ✅ Unique index created")
        else:
            print("  ✅ Unique index exists")

        # 2. Check salary_payments table
        print("\nChecking salary_payments table...")
        table_exists = await conn.fetchval("SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'salary_payments')")
        
        if not table_exists:
            print("  ➕ Creating table: salary_payments")
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS salary_payments (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    tenant_member_id UUID NOT NULL REFERENCES tenant_members(id) ON DELETE CASCADE,
                    period_month VARCHAR(7) NOT NULL,
                    payment_amount NUMERIC(12,2) NOT NULL,
                    payment_method VARCHAR(50),
                    payment_reference VARCHAR(255),
                    payment_date TIMESTAMP WITH TIME ZONE NOT NULL,
                    notes TEXT,
                    created_by UUID REFERENCES profile(id),
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """)
            print("  ✅ Table created")
        else:
            print("  ✅ Table exists")

        # 3. Check salary_attachments table
        print("\nChecking salary_attachments table...")
        table_exists = await conn.fetchval("SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'salary_attachments')")
        
        if not table_exists:
            print("  ➕ Creating table: salary_attachments")
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS salary_attachments (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    salary_payment_id UUID NOT NULL REFERENCES salary_payments(id) ON DELETE CASCADE,
                    path VARCHAR(500) NOT NULL,
                    file_name VARCHAR(255) NOT NULL,
                    file_size INTEGER,
                    mime_type VARCHAR(100),
                    s3_key VARCHAR(500),
                    uploaded_by UUID REFERENCES profile(id),
                    uploaded_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """)
            print("  ✅ Table created")
        else:
            print("  ✅ Table exists")

        print("\n✅ Verification and fix completed successfully!")

    except Exception as e:
        print(f"❌ Error during verification/fix: {e}")
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(verify_and_fix_db())

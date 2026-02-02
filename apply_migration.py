
import asyncio
import asyncpg
from app.config import settings

async def apply_migration():
    """Apply 002_salary_payments.sql migration"""
    conn = await asyncpg.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name
    )

    try:
        print("🔧 Applying migration 002_salary_payments.sql...")
        
        with open('migrations/002_salary_payments.sql', 'r') as f:
            sql = f.read()
            
        await conn.execute(sql)
        print("✅ Migration applied successfully")
        
    except Exception as e:
        print(f"❌ Error applying migration: {e}")
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(apply_migration())

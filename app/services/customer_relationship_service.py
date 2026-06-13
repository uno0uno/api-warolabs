from uuid import UUID


async def upsert_tenant_customer(conn, profile_id: UUID, tenant_id: UUID) -> None:
    await conn.execute(
        """
        INSERT INTO tenant_customers (tenant_id, profile_id, is_active)
        VALUES ($1, $2, true)
        ON CONFLICT (tenant_id, profile_id) DO UPDATE
        SET is_active = true,
            updated_at = NOW()
        """,
        tenant_id,
        profile_id,
    )


async def is_tenant_customer(conn, profile_id: UUID, tenant_id: UUID) -> bool:
    row = await conn.fetchval(
        """
        SELECT 1
        FROM tenant_customers
        WHERE profile_id = $1
          AND tenant_id = $2
          AND is_active = true
        """,
        profile_id,
        tenant_id,
    )
    return bool(row)

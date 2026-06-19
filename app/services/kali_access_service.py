"""Internal Kali feature availability by tenant."""
from typing import Dict, Optional
from uuid import UUID

from app.database import get_db_connection


async def is_kali_enabled(tenant_id: Optional[UUID]) -> bool:
    """Return whether Kali is enabled for a tenant.

    Missing tenant IDs, missing tenants, and missing public-profile rows are
    treated as disabled. Kali is intentionally not a RBAC module.
    """
    if tenant_id is None:
        return False

    async with get_db_connection() as conn:
        enabled = await conn.fetchval(
            """
            SELECT COALESCE(tpp.kali_enabled, false)
            FROM tenants t
            LEFT JOIN tenant_public_profiles tpp ON tpp.tenant_id = t.id
            WHERE t.id = $1
            """,
            tenant_id,
        )

    return bool(enabled)


async def get_kali_access_features(tenant_id: Optional[UUID]) -> Dict[str, bool]:
    """Capability payload consumed by /me/access and frontend route gates."""
    return {"kali_enabled": await is_kali_enabled(tenant_id)}

"""Public Table QR token resolution (warocol.com#710 / api-warolabs#266).

Resolves an opaque `qr_public_token` to tenant/table metadata for the
customer-facing menu route. No session required.
"""
from typing import Any, Dict, Optional

from app.database import get_db_connection


async def resolve_table_qr_token(token: str) -> Optional[Dict[str, Any]]:
    """Return public metadata for an active QR link, or None if inactive/unknown."""
    async with get_db_connection(use_transaction=False) as conn:
        row = await conn.fetchrow(
            """
            SELECT
                t.name AS table_name,
                t.qr_enabled,
                t.is_active AS table_active,
                tpp.slug AS tenant_slug,
                tpp.display_name,
                tpp.table_qr_module_enabled,
                tpp.is_active AS profile_active
            FROM tables t
            JOIN tenant_public_profiles tpp ON tpp.tenant_id = t.tenant_id
            WHERE t.qr_public_token = $1
              AND t.deleted_at IS NULL
            """,
            token,
        )

    if not row:
        return None

    if (
        not row["table_qr_module_enabled"]
        or not row["qr_enabled"]
        or not row["profile_active"]
        or not row["table_active"]
    ):
        return None

    return {
        "tenant_slug": row["tenant_slug"],
        "display_name": row["display_name"],
        "table_name": row["table_name"],
        "table_qr_module_enabled": bool(row["table_qr_module_enabled"]),
        "qr_enabled": bool(row["qr_enabled"]),
    }

"""Operaciones audience aggregator + toggle writes.

Mirrors the POS audience pattern (see pos_context_service): a single GET
aggregator exposed under Module.OPERACIONES, plus dedicated PATCH helpers
for the five operational toggles that supervisor/admin flip day-to-day from
/operaciones/{comandas,mesas,personalizar}.

The read payload is identical to the POS one — we delegate to
`pos_context_service.get_restaurant_context` to keep one source of truth.
Separate symbols + endpoint paths preserve clean audience boundaries (and
make future divergence local to this module).

The toggle writer parameterizes the column name dynamically; the
`ALLOWED_TOGGLES` whitelist prevents SQL injection.
"""
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import HTTPException

from app.database import get_db_connection
from app.services.pos_context_service import get_restaurant_context as _pos_get_context


ALLOWED_TOGGLES = frozenset({
    "kds_enabled",
    "comandas_enabled",
    "expediter_enabled",
    "tables_enabled",
    "auto_select_generic_enabled",
})


async def get_operaciones_context(tenant_id: UUID) -> Optional[Dict[str, Any]]:
    """Aggregated tenant context for the operaciones audience.

    Same payload as the POS aggregator — supervisor/admin pages read the
    same toggles and metadata as cashiers do, just at a different scope.
    """
    return await _pos_get_context(tenant_id)


async def update_toggle(
    tenant_id: UUID,
    column_name: str,
    enabled: bool,
) -> Dict[str, Any]:
    """Update a single boolean toggle on `tenant_public_profiles`.

    `column_name` is dynamic but validated against `ALLOWED_TOGGLES`; any
    value outside the whitelist raises 422 before any SQL runs. The UPSERT
    keeps behaviour parity with `tenant_config_service.upsert_public_profile`
    in case the row doesn't exist yet (fresh tenant).
    """
    if column_name not in ALLOWED_TOGGLES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown toggle: {column_name}",
        )

    # Safe: column_name is validated against a hardcoded whitelist above.
    query = f"""
        INSERT INTO tenant_public_profiles (tenant_id, {column_name})
        VALUES ($1, $2)
        ON CONFLICT (tenant_id) DO UPDATE
            SET {column_name} = EXCLUDED.{column_name},
                updated_at = now()
    """

    async with get_db_connection() as conn:
        await conn.execute(query, tenant_id, enabled)

    return {"success": True, "data": {column_name: enabled}}

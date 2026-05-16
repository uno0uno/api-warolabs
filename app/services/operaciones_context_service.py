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
    "waiter_attribution_enabled",  # warocol.com#573
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
    #
    # Hotfix (post-#573 family): the original UPSERT only inserted
    # `tenant_id` + the toggle column, which fails for fresh tenants
    # that don't have a `tenant_public_profiles` row yet because
    # `slug` and `display_name` are NOT NULL with no defaults.
    # We seed those required fields from the `tenants` table (which
    # ALWAYS has both populated). For existing rows the ON CONFLICT
    # branch ignores the seed values — only the toggle column is
    # updated, so this is non-destructive for tenants with a real
    # profile already configured.
    query = f"""
        INSERT INTO tenant_public_profiles (tenant_id, slug, display_name, {column_name})
        SELECT t.id, t.slug, t.name, $2
        FROM tenants t
        WHERE t.id = $1
        ON CONFLICT (tenant_id) DO UPDATE
            SET {column_name} = EXCLUDED.{column_name},
                updated_at = now()
    """

    async with get_db_connection() as conn:
        await conn.execute(query, tenant_id, enabled)

    return {"success": True, "data": {column_name: enabled}}


async def update_tables_label(
    tenant_id: UUID,
    singular: Optional[str],
    plural: Optional[str],
) -> Dict[str, Any]:
    """Persist tenant-global custom labels for the mesa noun (warocol.com#614).

    Empty / whitespace inputs are normalized to NULL — the frontend
    interprets that as "use defaults" (Mesa / Mesas).

    Mirrors the UPSERT pattern from `update_toggle()` so fresh tenants
    without a tenant_public_profiles row still work; `slug` and
    `display_name` are seeded from the `tenants` table (both NOT NULL
    with no DB defaults).

    String-valued sibling to `update_toggle()`; deliberately NOT entered
    into the `ALLOWED_TOGGLES` whitelist since that frozenset is for
    boolean toggles only.
    """
    # Normalize: empty / whitespace-only -> NULL (resets to default)
    sin = singular.strip() if singular and singular.strip() else None
    plu = plural.strip() if plural and plural.strip() else None

    query = """
        INSERT INTO tenant_public_profiles
            (tenant_id, slug, display_name, tables_label_singular, tables_label_plural)
        SELECT t.id, t.slug, t.name, $2, $3
        FROM tenants t
        WHERE t.id = $1
        ON CONFLICT (tenant_id) DO UPDATE
            SET tables_label_singular = EXCLUDED.tables_label_singular,
                tables_label_plural   = EXCLUDED.tables_label_plural,
                updated_at = now()
        RETURNING tables_label_singular, tables_label_plural
    """

    async with get_db_connection() as conn:
        row = await conn.fetchrow(query, tenant_id, sin, plu)

    if row is None:
        raise HTTPException(status_code=404, detail="Tenant not found")

    return {
        "success": True,
        "data": {
            "tables_label_singular": row["tables_label_singular"],
            "tables_label_plural": row["tables_label_plural"],
        },
    }


async def assert_waiter_attribution_enabled(tenant_id: UUID) -> None:
    """Raise 409 if `waiter_attribution_enabled` is False for this tenant.

    Used by mutation endpoints in the waiter-attribution family
    (warocol.com#573/#574/#575) to reject writes when the owner has the
    feature disabled — even if the caller bypasses the UI gate.
    """
    async with get_db_connection(use_transaction=False) as conn:
        row = await conn.fetchrow(
            "SELECT waiter_attribution_enabled FROM tenant_public_profiles WHERE tenant_id = $1",
            tenant_id,
        )
    if not row or not row["waiter_attribution_enabled"]:
        raise HTTPException(
            status_code=409,
            detail="Waiter attribution is disabled for this tenant",
        )

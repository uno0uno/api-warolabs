"""POS restaurant-context aggregator.

Returns the minimal subset of `/api/tenant/*` data that POS pages need to
render and complete sales: display name, KDS / comandas toggles, fiscal
identity, tax flags, invoicing readiness. Exposed via a single endpoint
gated under Module.POS so cashiers reach it without needing MI_NEGOCIO
(which stays owner-only).

Single query joins four tables; everything POS needs in one round-trip.
"""
from typing import Any, Dict, Optional
from uuid import UUID

from app.database import get_db_connection
from app.services.invoicing_readiness_service import get_readiness


_CONTEXT_QUERY = """
SELECT
    tpp.display_name,
    tpp.kds_enabled,
    tpp.comandas_enabled,
    tpp.expediter_enabled,
    tpp.tables_enabled,
    tpp.accepts_online_orders,
    tpp.auto_select_generic_enabled,
    tpp.waiter_attribution_enabled,
    tpp.tables_label_singular,
    tpp.tables_label_plural,
    tpp.tip_enabled,
    tpp.tip_default_percentages,
    tpp.tip_preselect_index,
    fd.nit,
    fd.business_name,
    fd.type_organization_id,
    fd.tax_regime_id,
    fd.tax_level_id,
    fd.fiscal_address,
    fd.city,
    fd.city_id,
    fd.phone           AS fiscal_phone,
    fd.email           AS fiscal_email,
    ttc.inc_applicable,
    ttc.inc_included_in_price,
    ttc.iva_applicable,
    ttc.iva_included_in_price,
    ttc.liquor_tax_applicable
FROM tenants t
LEFT JOIN tenant_public_profiles tpp ON tpp.tenant_id = t.id
LEFT JOIN tenant_fiscal_data      fd  ON fd.tenant_id  = t.id
LEFT JOIN tenant_tax_config       ttc ON ttc.tenant_id = t.id
WHERE t.id = $1
"""


_MEMBERS_QUERY = """
SELECT tm.id, p.name, tm.role
FROM tenant_members tm
JOIN profile p ON p.id = tm.user_id
WHERE tm.tenant_id = $1
  AND tm.is_active = true
  AND tm.terminated_at IS NULL
  AND tm.role IN ('superuser', 'admin', 'employee', 'member')
ORDER BY p.name
"""


async def get_restaurant_context(tenant_id: UUID) -> Optional[Dict[str, Any]]:
    """Aggregate the POS-relevant tenant data.

    Returns None if the tenant does not exist. Missing rows in the joined
    tables surface as None values inside the payload — POS pages handle
    that gracefully (defaults / blank fields).

    Includes a `members` list (active tenant members) for the waiter-
    attribution family (warocol.com#573/#574/#575). Embedded here so
    cashiers/supervisors can populate waiter dropdowns without needing
    Module.EQUIPO access (which gates /api/tenants/members).
    """
    async with get_db_connection(use_transaction=False) as conn:
        row = await conn.fetchrow(_CONTEXT_QUERY, tenant_id)
        if row is None:
            return None
        members_rows = await conn.fetch(_MEMBERS_QUERY, tenant_id)

    readiness = await get_readiness(tenant_id)
    invoicing_ready = bool(readiness and readiness.get('ready'))

    return {
        'display_name': row['display_name'],
        'kds_enabled': bool(row['kds_enabled']) if row['kds_enabled'] is not None else False,
        'comandas_enabled': bool(row['comandas_enabled']) if row['comandas_enabled'] is not None else False,
        'expediter_enabled': bool(row['expediter_enabled']) if row['expediter_enabled'] is not None else False,
        'tables_enabled': bool(row['tables_enabled']) if row['tables_enabled'] is not None else False,
        'accepts_online_orders': bool(row['accepts_online_orders']) if row['accepts_online_orders'] is not None else False,
        'auto_select_generic_enabled': bool(row['auto_select_generic_enabled']) if row['auto_select_generic_enabled'] is not None else False,
        'waiter_attribution_enabled': bool(row['waiter_attribution_enabled']) if row['waiter_attribution_enabled'] is not None else False,
        'tables_label_singular': row['tables_label_singular'],
        'tables_label_plural': row['tables_label_plural'],
        'tip_enabled': bool(row['tip_enabled']) if row['tip_enabled'] is not None else False,
        'tip_default_percentages': (
            [float(p) for p in row['tip_default_percentages']]
            if row['tip_default_percentages'] else [10.0]
        ),
        'tip_preselect_index': row['tip_preselect_index'],
        'fiscal_data': {
            'nit': row['nit'],
            'business_name': row['business_name'],
            'type_organization_id': row['type_organization_id'],
            'tax_regime_id': row['tax_regime_id'],
            'tax_level_id': row['tax_level_id'],
            'fiscal_address': row['fiscal_address'],
            'city': row['city'],
            'city_id': row['city_id'],
            'phone': row['fiscal_phone'],
            'email': row['fiscal_email'],
        },
        'tax_config': {
            'inc_applicable': bool(row['inc_applicable']) if row['inc_applicable'] is not None else False,
            'inc_included_in_price': bool(row['inc_included_in_price']) if row['inc_included_in_price'] is not None else False,
            'iva_applicable': bool(row['iva_applicable']) if row['iva_applicable'] is not None else False,
            'iva_included_in_price': bool(row['iva_included_in_price']) if row['iva_included_in_price'] is not None else False,
            'liquor_tax_applicable': bool(row['liquor_tax_applicable']) if row['liquor_tax_applicable'] is not None else False,
        },
        'invoicing_ready': invoicing_ready,
        'members': [
            {
                'id': str(r['id']),
                'name': r['name'] or 'Sin nombre',
                'role': r['role'],
            }
            for r in members_rows
        ],
    }

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
from app.services.open_priced_service import fetch_open_sale_product
from app.services.promotions_service import (
    DEFAULT_PROMO_CONFLICT_STRATEGY,
    normalize_promo_type_block_map,
)


_CONTEXT_QUERY = """
SELECT
    tpp.display_name,
    tpp.kds_enabled,
    tpp.comandas_enabled,
    tpp.expediter_enabled,
    tpp.tables_enabled,
    tpp.table_qr_module_enabled,
    tpp.accepts_online_orders,
    tpp.auto_select_generic_enabled,
    tpp.open_sale_enabled,
    tpp.waiter_attribution_enabled,
    tpp.tables_label_singular,
    tpp.tables_label_plural,
    tpp.tip_enabled,
    tpp.tip_taxable_default,
    tpp.tip_default_percentages,
    tpp.tip_preselect_index,
    tpp.logo_url,
    tpp.allow_promo_line_opt_out,
    tpp.promo_conflict_strategy,
    tpp.promo_type_block_map,
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
    fd.receipt_document_label,
    fd.receipt_tip_label,
    fd.show_logo_on_receipts,
    ttc.inc_applicable,
    ttc.inc_rate,
    ttc.inc_included_in_price,
    ttc.iva_applicable,
    ttc.iva_rate,
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
        open_sale_product = await fetch_open_sale_product(conn, tenant_id)

    readiness = await get_readiness(tenant_id)
    invoicing_ready = bool(readiness and readiness.get('ready'))

    return {
        'display_name': row['display_name'],
        'kds_enabled': bool(row['kds_enabled']) if row['kds_enabled'] is not None else False,
        'comandas_enabled': bool(row['comandas_enabled']) if row['comandas_enabled'] is not None else False,
        'expediter_enabled': bool(row['expediter_enabled']) if row['expediter_enabled'] is not None else False,
        'tables_enabled': bool(row['tables_enabled']) if row['tables_enabled'] is not None else False,
        'table_qr_module_enabled': bool(row['table_qr_module_enabled']) if row['table_qr_module_enabled'] is not None else False,
        'accepts_online_orders': bool(row['accepts_online_orders']) if row['accepts_online_orders'] is not None else False,
        'auto_select_generic_enabled': bool(row['auto_select_generic_enabled']) if row['auto_select_generic_enabled'] is not None else False,
        'open_sale_enabled': bool(row['open_sale_enabled']) if row['open_sale_enabled'] is not None else False,
        'waiter_attribution_enabled': bool(row['waiter_attribution_enabled']) if row['waiter_attribution_enabled'] is not None else False,
        'tables_label_singular': row['tables_label_singular'],
        'tables_label_plural': row['tables_label_plural'],
        'tip_enabled': bool(row['tip_enabled']) if row['tip_enabled'] is not None else False,
        'tip_taxable_default': bool(row['tip_taxable_default']) if row['tip_taxable_default'] is not None else False,
        'tip_default_percentages': (
            [float(p) for p in row['tip_default_percentages']]
            if row['tip_default_percentages'] else [10.0]
        ),
        'tip_preselect_index': row['tip_preselect_index'],
        'allow_promo_line_opt_out': bool(row['allow_promo_line_opt_out'])
        if row['allow_promo_line_opt_out'] is not None
        else False,
        'promo_conflict_strategy': (
            row['promo_conflict_strategy']
            if row['promo_conflict_strategy'] is not None
            else DEFAULT_PROMO_CONFLICT_STRATEGY
        ),
        'promo_type_block_map': normalize_promo_type_block_map(
            row['promo_type_block_map']
        ),
        'logo_url': row['logo_url'],
        'receipt_print_settings': {
            'document_label': (row['receipt_document_label'] or 'Prefactura').strip()[:40],
            'tip_label': (row['receipt_tip_label'] or 'Propina').strip()[:40],
            'show_logo': bool(row['show_logo_on_receipts'])
            if row['show_logo_on_receipts'] is not None
            else True,
        },
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
            'inc_rate': float(row['inc_rate']) if row['inc_rate'] is not None else 0.08,
            'inc_included_in_price': bool(row['inc_included_in_price']) if row['inc_included_in_price'] is not None else False,
            'iva_applicable': bool(row['iva_applicable']) if row['iva_applicable'] is not None else False,
            'iva_rate': float(row['iva_rate']) if row['iva_rate'] is not None else 0.19,
            'iva_included_in_price': bool(row['iva_included_in_price']) if row['iva_included_in_price'] is not None else False,
            'liquor_tax_applicable': bool(row['liquor_tax_applicable']) if row['liquor_tax_applicable'] is not None else False,
        },
        'invoicing_ready': invoicing_ready,
        'open_sale_product': open_sale_product,
        'members': [
            {
                'id': str(r['id']),
                'name': r['name'] or 'Sin nombre',
                'role': r['role'],
            }
            for r in members_rows
        ],
    }

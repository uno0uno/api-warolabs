"""POS restaurant-context aggregator.

Returns the minimal subset of `/api/tenant/*` data that POS pages need to
render and complete sales: display name, KDS / comandas toggles, fiscal
identity, tax flags, invoicing readiness. Exposed via a single endpoint
gated under Module.POS so cashiers reach it without needing MI_NEGOCIO
(which stays owner-only).

Single query joins four tables; everything POS needs in one round-trip.
"""
import logging
from typing import Any, Dict, Optional
from uuid import UUID

import asyncpg

from app.database import get_db_connection
from app.core.platform_legal import get_platform_legal_for_print
from app.core.timezones import normalize_timezone
from app.core.tenant_prefs import (
    normalize_currency_code,
    normalize_locale,
    normalize_ui_locale,
)
from app.services.invoicing_readiness_service import get_readiness
from app.services.open_priced_service import fetch_open_sale_product
from app.services.promotions_service import (
    DEFAULT_PROMO_CONFLICT_STRATEGY,
    normalize_promo_type_block_map,
)

logger = logging.getLogger(__name__)


_CONTEXT_QUERY = """
SELECT
    tpp.display_name,
    tpp.timezone,
    tpp.locale,
    tpp.ui_locale,
    tpp.currency_code,
    tpp.kds_enabled,
    tpp.comandas_enabled,
    tpp.expediter_enabled,
    tpp.tables_enabled,
    tpp.table_qr_module_enabled,
    tpp.accepts_online_orders,
    tpp.auto_select_generic_enabled,
    tpp.open_sale_enabled,
    tpp.minimum_consumption_enabled,
    tpp.minimum_consumption_amount,
    tpp.minimum_consumption_restrictive,
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
    tpp.pos_catalog_layout_default,
    tpp.pos_show_product_image,
    tpp.pos_show_search,
    tpp.deduct_inventory_on_command,
    tpp.hide_products_without_stock,
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
    ttc.liquor_tax_applicable,
    ttc.liquor_tax_rate,
    ttc.liquor_tax_included_in_price,
    ttc.tax_lines,
    ttc.category_map,
    ttc.commercial_tax_applicable,
    ttc.menu_category_line_map,
    ttc.exempt_menu_category_ids
FROM tenants t
LEFT JOIN tenant_public_profiles tpp ON tpp.tenant_id = t.id
LEFT JOIN tenant_fiscal_data      fd  ON fd.tenant_id  = t.id
LEFT JOIN tenant_tax_config       ttc ON ttc.tenant_id = t.id
WHERE t.id = $1
"""

# Preserve existing preferences when only migration 100 (ui_locale) is pending.
_CONTEXT_QUERY_WITHOUT_UI_LOCALE = _CONTEXT_QUERY.replace(
    "    tpp.ui_locale,\n",
    "    NULL AS ui_locale,\n",
)

# When #2495 migration is pending, keep POS context working with defaults.
_CONTEXT_QUERY_WITHOUT_POS_CATALOG = (
    _CONTEXT_QUERY
    .replace("    tpp.pos_catalog_layout_default,\n", "    NULL AS pos_catalog_layout_default,\n")
    .replace("    tpp.pos_show_product_image,\n", "    NULL AS pos_show_product_image,\n")
    .replace("    tpp.pos_show_search,\n", "    NULL AS pos_show_search,\n")
    .replace("    tpp.deduct_inventory_on_command,\n", "    NULL AS deduct_inventory_on_command,\n")
    .replace("    tpp.hide_products_without_stock,\n", "    NULL AS hide_products_without_stock,\n")
)
_CONTEXT_QUERY_WITHOUT_UI_LOCALE_OR_POS_CATALOG = (
    _CONTEXT_QUERY_WITHOUT_UI_LOCALE
    .replace("    tpp.pos_catalog_layout_default,\n", "    NULL AS pos_catalog_layout_default,\n")
    .replace("    tpp.pos_show_product_image,\n", "    NULL AS pos_show_product_image,\n")
    .replace("    tpp.pos_show_search,\n", "    NULL AS pos_show_search,\n")
    .replace("    tpp.deduct_inventory_on_command,\n", "    NULL AS deduct_inventory_on_command,\n")
    .replace("    tpp.hide_products_without_stock,\n", "    NULL AS hide_products_without_stock,\n")
)

# warocol.com#2574 — newest column; fall back before older prefs
_CONTEXT_QUERY_WITHOUT_HIDE_WITHOUT_STOCK = _CONTEXT_QUERY.replace(
    "    tpp.hide_products_without_stock,\n",
    "    NULL AS hide_products_without_stock,\n",
)

# warocol.com#2566 — column may land after catalog prefs
_CONTEXT_QUERY_WITHOUT_DEDUCT_ON_COMMAND = _CONTEXT_QUERY_WITHOUT_HIDE_WITHOUT_STOCK.replace(
    "    tpp.deduct_inventory_on_command,\n",
    "    NULL AS deduct_inventory_on_command,\n",
)

# Legacy fallback when older preference migrations are also pending.
_CONTEXT_QUERY_WITHOUT_PREFS = (
    _CONTEXT_QUERY_WITHOUT_POS_CATALOG
    .replace("    tpp.timezone,\n", "    NULL AS timezone,\n")
    .replace("    tpp.locale,\n", "    NULL AS locale,\n")
    .replace("    tpp.ui_locale,\n", "    NULL AS ui_locale,\n")
    .replace("    tpp.currency_code,\n", "    NULL AS currency_code,\n")
)
# Backward-compatible alias used by older call sites / greps.
_CONTEXT_QUERY_WITHOUT_TIMEZONE = _CONTEXT_QUERY_WITHOUT_PREFS


_MEMBERS_QUERY = """
SELECT tm.id, p.name, tm.role
FROM tenant_members tm
JOIN profile p ON p.id = tm.user_id
WHERE tm.tenant_id = $1
  AND tm.is_active = true
  AND tm.terminated_at IS NULL
  AND tm.role IN ('superuser', 'admin', 'employee', 'member', 'promotor')
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
        try:
            row = await conn.fetchrow(_CONTEXT_QUERY, tenant_id)
        except asyncpg.UndefinedColumnError as exc:
            missing = str(exc)
            if "hide_products_without_stock" in missing:
                logger.warning(
                    "hide_products_without_stock missing in POS context; "
                    "defaulting false (warocol.com#2574)."
                )
                try:
                    row = await conn.fetchrow(
                        _CONTEXT_QUERY_WITHOUT_HIDE_WITHOUT_STOCK,
                        tenant_id,
                    )
                except asyncpg.UndefinedColumnError as nested:
                    missing = str(nested)
                    if "deduct_inventory_on_command" in missing:
                        row = await conn.fetchrow(
                            _CONTEXT_QUERY_WITHOUT_DEDUCT_ON_COMMAND,
                            tenant_id,
                        )
                    elif "pos_catalog" in missing or "pos_show_" in missing:
                        row = await conn.fetchrow(
                            _CONTEXT_QUERY_WITHOUT_POS_CATALOG,
                            tenant_id,
                        )
                    else:
                        row = await conn.fetchrow(
                            _CONTEXT_QUERY_WITHOUT_UI_LOCALE_OR_POS_CATALOG
                            if "ui_locale" in missing
                            else _CONTEXT_QUERY_WITHOUT_PREFS,
                            tenant_id,
                        )
            elif "deduct_inventory_on_command" in missing:
                logger.warning(
                    "deduct_inventory_on_command missing in POS context; "
                    "defaulting false (warocol.com#2572)."
                )
                try:
                    row = await conn.fetchrow(
                        _CONTEXT_QUERY_WITHOUT_DEDUCT_ON_COMMAND,
                        tenant_id,
                    )
                except asyncpg.UndefinedColumnError as nested:
                    missing = str(nested)
                    if "pos_catalog" in missing or "pos_show_" in missing:
                        row = await conn.fetchrow(
                            _CONTEXT_QUERY_WITHOUT_POS_CATALOG,
                            tenant_id,
                        )
                    else:
                        row = await conn.fetchrow(
                            _CONTEXT_QUERY_WITHOUT_UI_LOCALE_OR_POS_CATALOG
                            if "ui_locale" in missing
                            else _CONTEXT_QUERY_WITHOUT_PREFS,
                            tenant_id,
                        )
            elif "pos_catalog" in missing or "pos_show_" in missing:
                logger.warning(
                    "POS catalog preference columns missing in POS context; "
                    "using defaults until warocol.com#2495 migration is applied."
                )
                try:
                    row = await conn.fetchrow(
                        _CONTEXT_QUERY_WITHOUT_POS_CATALOG,
                        tenant_id,
                    )
                except asyncpg.UndefinedColumnError:
                    logger.warning(
                        "tenant_public_profiles.ui_locale missing in POS context; "
                        "preserving existing tenant preferences until migration 100."
                    )
                    try:
                        row = await conn.fetchrow(
                            _CONTEXT_QUERY_WITHOUT_UI_LOCALE_OR_POS_CATALOG,
                            tenant_id,
                        )
                    except asyncpg.UndefinedColumnError:
                        logger.warning(
                            "Older tenant preference columns also missing in POS context; "
                            "using safe defaults until migrations 095/099 are applied."
                        )
                        row = await conn.fetchrow(
                            _CONTEXT_QUERY_WITHOUT_PREFS, tenant_id
                        )
            else:
                logger.warning(
                    "tenant_public_profiles.ui_locale missing in POS context; "
                    "preserving existing tenant preferences until migration 100."
                )
                try:
                    row = await conn.fetchrow(
                        _CONTEXT_QUERY_WITHOUT_UI_LOCALE,
                        tenant_id,
                    )
                except asyncpg.UndefinedColumnError as inner_exc:
                    inner = str(inner_exc)
                    if "hide_products_without_stock" in inner:
                        row = await conn.fetchrow(
                            _CONTEXT_QUERY_WITHOUT_UI_LOCALE.replace(
                                "    tpp.hide_products_without_stock,\n",
                                "    NULL AS hide_products_without_stock,\n",
                            ),
                            tenant_id,
                        )
                    elif "deduct_inventory_on_command" in inner:
                        row = await conn.fetchrow(
                            _CONTEXT_QUERY_WITHOUT_UI_LOCALE.replace(
                                "    tpp.hide_products_without_stock,\n",
                                "    NULL AS hide_products_without_stock,\n",
                            ).replace(
                                "    tpp.deduct_inventory_on_command,\n",
                                "    NULL AS deduct_inventory_on_command,\n",
                            ),
                            tenant_id,
                        )
                    elif "pos_catalog" in inner or "pos_show_" in inner:
                        row = await conn.fetchrow(
                            _CONTEXT_QUERY_WITHOUT_UI_LOCALE_OR_POS_CATALOG,
                            tenant_id,
                        )
                    else:
                        logger.warning(
                            "Older tenant preference columns also missing in POS context; "
                            "using safe defaults until migrations 095/099 are applied."
                        )
                        row = await conn.fetchrow(
                            _CONTEXT_QUERY_WITHOUT_PREFS, tenant_id
                        )
        if row is None:
            return None
        members_rows = await conn.fetch(_MEMBERS_QUERY, tenant_id)
        open_sale_product = await fetch_open_sale_product(conn, tenant_id)

    readiness = await get_readiness(tenant_id)
    invoicing_ready = bool(readiness and readiness.get('ready'))

    from app.services.tenant_config_service import decode_tax_config_jsonb

    tax_config_raw = {
        'inc_applicable': bool(row['inc_applicable']) if row['inc_applicable'] is not None else False,
        'inc_rate': float(row['inc_rate']) if row['inc_rate'] is not None else 0.08,
        'inc_included_in_price': bool(row['inc_included_in_price']) if row['inc_included_in_price'] is not None else False,
        'iva_applicable': bool(row['iva_applicable']) if row['iva_applicable'] is not None else False,
        'iva_rate': float(row['iva_rate']) if row['iva_rate'] is not None else 0.19,
        'iva_included_in_price': bool(row['iva_included_in_price']) if row['iva_included_in_price'] is not None else False,
        'liquor_tax_applicable': bool(row['liquor_tax_applicable']) if row['liquor_tax_applicable'] is not None else False,
        'liquor_tax_rate': (
            float(row['liquor_tax_rate'])
            if ('liquor_tax_rate' in row.keys() and row['liquor_tax_rate'] is not None)
            else 0.05
        ),
        'liquor_tax_included_in_price': (
            bool(row['liquor_tax_included_in_price'])
            if ('liquor_tax_included_in_price' in row.keys() and row['liquor_tax_included_in_price'] is not None)
            else False
        ),
        'tax_lines': row['tax_lines'] if 'tax_lines' in row.keys() else None,
        'category_map': row['category_map'] if 'category_map' in row.keys() else None,
        'commercial_tax_applicable': (
            bool(row['commercial_tax_applicable'])
            if ('commercial_tax_applicable' in row.keys() and row['commercial_tax_applicable'] is not None)
            else None
        ),
        'menu_category_line_map': (
            row['menu_category_line_map'] if 'menu_category_line_map' in row.keys() else None
        ),
        'exempt_menu_category_ids': (
            row['exempt_menu_category_ids'] if 'exempt_menu_category_ids' in row.keys() else None
        ),
    }
    tax_config = decode_tax_config_jsonb(tax_config_raw)

    return {
        'display_name': row['display_name'],
        'timezone': normalize_timezone(row['timezone'] if 'timezone' in row else None),
        # Prefer .get-style access so unit mocks without prefs columns don't KeyError.
        'locale': normalize_locale(row['locale'] if 'locale' in row else None),
        'ui_locale': normalize_ui_locale(
            row['ui_locale'] if 'ui_locale' in row else None
        ),
        'currency_code': normalize_currency_code(
            row['currency_code'] if 'currency_code' in row else None
        ),
        'kds_enabled': bool(row['kds_enabled']) if row['kds_enabled'] is not None else False,
        'comandas_enabled': bool(row['comandas_enabled']) if row['comandas_enabled'] is not None else False,
        'expediter_enabled': bool(row['expediter_enabled']) if row['expediter_enabled'] is not None else False,
        'tables_enabled': bool(row['tables_enabled']) if row['tables_enabled'] is not None else False,
        'table_qr_module_enabled': bool(row['table_qr_module_enabled']) if row['table_qr_module_enabled'] is not None else False,
        'accepts_online_orders': bool(row['accepts_online_orders']) if row['accepts_online_orders'] is not None else False,
        'auto_select_generic_enabled': bool(row['auto_select_generic_enabled']) if row['auto_select_generic_enabled'] is not None else False,
        'open_sale_enabled': bool(row['open_sale_enabled']) if row['open_sale_enabled'] is not None else False,
        'minimum_consumption_enabled': bool(row['minimum_consumption_enabled'])
        if row['minimum_consumption_enabled'] is not None
        else False,
        'minimum_consumption_amount': (
            float(row['minimum_consumption_amount'])
            if row['minimum_consumption_amount'] is not None
            else 0.0
        ),
        'minimum_consumption_restrictive': bool(row['minimum_consumption_restrictive'])
        if row['minimum_consumption_restrictive'] is not None
        else False,
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
        # warocol.com#2495 — POS catalog presentation defaults (tenant-wide)
        'pos_catalog_layout_default': (
            row['pos_catalog_layout_default']
            if (
                'pos_catalog_layout_default' in row.keys()
                and row['pos_catalog_layout_default'] in ('grid', 'list')
            )
            else 'grid'
        ),
        'pos_show_product_image': (
            bool(row['pos_show_product_image'])
            if (
                'pos_show_product_image' in row.keys()
                and row['pos_show_product_image'] is not None
            )
            else True
        ),
        'pos_show_search': (
            bool(row['pos_show_search'])
            if ('pos_show_search' in row.keys() and row['pos_show_search'] is not None)
            else True
        ),
        'deduct_inventory_on_command': (
            bool(row['deduct_inventory_on_command'])
            if (
                'deduct_inventory_on_command' in row.keys()
                and row['deduct_inventory_on_command'] is not None
            )
            else False
        ),
        'hide_products_without_stock': (
            bool(row['hide_products_without_stock'])
            if (
                'hide_products_without_stock' in row.keys()
                and row['hide_products_without_stock'] is not None
            )
            else False
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
        'tax_config': tax_config,
        'invoicing_ready': invoicing_ready,
        # WARO software + Matias facturador labels for tickets (env-driven; not tenant issuer)
        'platform_legal': get_platform_legal_for_print(),
        # PDF retrieval and email attachment are always enabled.
        'invoice_pdf_enabled': True,
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

"""
Invoicing readiness service — issue #130

Single source of truth for "can this tenant emit electronic invoices right now?"
Four predicates must all be true:

  1. dev_flag_enabled       — tenants.electronic_invoicing_enabled = true
                              (dev-only kill switch, set via SQL during onboarding)
  2. fiscal_data_complete   — tenant_fiscal_data has non-null nit, business_name,
                              phone, email
  3. active_resolution      — at least one row in dian_resolutions with
                              is_active=true, valid date range, available numbers,
                              and document_type='invoice'
  4. taxes_configured       — tenant_tax_config has at least one of inc_applicable
                              or iva_applicable set to true. In Colombian
                              hospitality, having both off almost always means
                              the tenant has not configured taxes yet — emitting
                              an invoice with no tax lines would surface a
                              misconfiguration to DIAN/Matías.

Returned shape (this is the public contract — frontend issue uno0uno/warocol.com#450
and microservice gate uno0uno/api-facturacion#17 must consume the same JSON):

    {
      "ready": bool,
      "checks": {
        "dev_flag_enabled":     bool,
        "fiscal_data_complete": bool,
        "active_resolution":    bool,
        "taxes_configured":     bool,
      },
      "missing": [str, ...],   # human-readable Spanish reasons
    }
"""
from typing import Any, Dict, List, Optional
from uuid import UUID

from app.database import get_db_connection


_READINESS_QUERY = """
SELECT
    t.electronic_invoicing_enabled  AS dev_flag_enabled,
    fd.nit                          AS nit,
    fd.business_name                AS business_name,
    fd.phone                        AS phone,
    fd.email                        AS email,
    COALESCE(ttc.inc_applicable, false) AS inc_applicable,
    COALESCE(ttc.iva_applicable, false) AS iva_applicable,
    EXISTS (
        SELECT 1
        FROM dian_resolutions r
        WHERE r.tenant_id      = t.id
          AND r.is_active      = true
          AND r.document_type  = 'invoice'
          AND CURRENT_DATE BETWEEN r.date_from AND r.date_to
          AND r.current_number < r.to_number
    ) AS active_resolution
FROM tenants t
LEFT JOIN tenant_fiscal_data fd ON fd.tenant_id = t.id
LEFT JOIN tenant_tax_config  ttc ON ttc.tenant_id = t.id
WHERE t.id = $1
"""


async def get_readiness(tenant_id: UUID) -> Optional[Dict[str, Any]]:
    """
    Return the readiness payload for the given tenant, or None if the tenant
    does not exist.
    """
    async with get_db_connection(use_transaction=False) as conn:
        row = await conn.fetchrow(_READINESS_QUERY, tenant_id)

    if row is None:
        return None

    dev_flag_enabled  = bool(row['dev_flag_enabled'])
    active_resolution = bool(row['active_resolution'])
    taxes_configured  = bool(row['inc_applicable']) or bool(row['iva_applicable'])

    missing_fiscal_fields: List[str] = []
    if row['nit'] is None:
        missing_fiscal_fields.append('NIT')
    if row['business_name'] is None:
        missing_fiscal_fields.append('razón social')
    if row['phone'] is None:
        missing_fiscal_fields.append('teléfono')
    if row['email'] is None:
        missing_fiscal_fields.append('email')
    fiscal_data_complete = len(missing_fiscal_fields) == 0

    missing: List[str] = []
    if not dev_flag_enabled:
        missing.append('Facturación electrónica deshabilitada por el equipo de WARO')
    if not fiscal_data_complete:
        missing.append(
            'Faltan datos fiscales: ' + ', '.join(missing_fiscal_fields)
        )
    if not active_resolution:
        missing.append('No hay una resolución DIAN vigente con numeración disponible')
    if not taxes_configured:
        missing.append('No hay impuestos configurados (activa INC o IVA en Configuración fiscal)')

    return {
        'ready': dev_flag_enabled and fiscal_data_complete and active_resolution and taxes_configured,
        'checks': {
            'dev_flag_enabled':     dev_flag_enabled,
            'fiscal_data_complete': fiscal_data_complete,
            'active_resolution':    active_resolution,
            'taxes_configured':     taxes_configured,
        },
        'missing': missing,
    }

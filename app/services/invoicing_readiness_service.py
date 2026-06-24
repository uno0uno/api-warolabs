"""
Invoicing readiness service — issue #130

Single source of truth for "can this tenant emit electronic invoices right now?"
Six predicates must all be true:

  1. customer_requested     — tenant_fiscal_data.electronic_invoicing_requested
                              = true (customer intent from /facturacion)
  2. dev_flag_enabled       — tenants.electronic_invoicing_enabled = true
                              (dev-only kill switch, set via SQL during onboarding)
  3. fiscal_data_complete   — tenant_fiscal_data has non-null nit, business_name,
                              phone, email
  4. active_resolution      — at least one row in dian_resolutions with
                              is_active=true, valid date range, available numbers,
                              and document_type='invoice'
  5. tax_requirement_satisfied
                            — tenant_tax_config has at least one of inc_applicable
                              or iva_applicable set to true, or the issuer fiscal
                              configuration supports no IVA/INC on sale lines.
  6. matias_company_id_configured
                            — Matias Casa de Software emissions have
                              tenant_fiscal_data.matias_company_id configured
                              for the outbound Matias client_uuid.

About the no-responsable bypass that was here briefly:

  Earlier iteration relaxed taxes_configured to allow tax_regime_id=2 (no
  responsable de IVA) to emit without INC/IVA. Empirical testing on
  2026-04-29 (LZT-3995/3996/3997) showed Matías does NOT return PDF or
  AttachedDocument when the factura has no tax line, so the customer
  email goes out without the PDF attachment — degraded UX.

  Current decision (warocol.com#1455): API WARO readiness may allow the
  supported no-responsable/persona-natural/no-tax scenario while #1456 keeps
  api-facturacion and Matias artifact validation explicit.

Returned shape (this is the public contract — frontend issue uno0uno/warocol.com#450
and microservice gate uno0uno/api-facturacion#17 must consume the same JSON):

    {
      "ready": bool,
      "checks": {
        "customer_requested": bool,
        "dev_flag_enabled":     bool,
        "fiscal_data_complete": bool,
        "active_resolution":    bool,
        "taxes_configured":     bool,  # compatibility: INC or IVA active
        "tax_requirement_satisfied": bool,
        "matias_company_id_configured": bool,
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
    t.slug                          AS tenant_slug,
    fd.nit                          AS nit,
    fd.business_name                AS business_name,
    fd.phone                        AS phone,
    fd.email                        AS email,
    COALESCE(fd.electronic_invoicing_requested, false)
                                    AS customer_requested,
    fd.matias_company_id            AS matias_company_id,
    fd.type_organization_id         AS type_organization_id,
    fd.tax_regime_id                AS tax_regime_id,
    fd.tax_level_id                 AS tax_level_id,
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

    customer_requested = bool(row['customer_requested'])
    dev_flag_enabled  = bool(row['dev_flag_enabled'])
    active_resolution = bool(row['active_resolution'])

    taxes_configured = bool(row['inc_applicable']) or bool(row['iva_applicable'])
    no_tax_allowed = (
        not taxes_configured
        and row['type_organization_id'] == 2
        and row['tax_regime_id'] == 2
        and row['tax_level_id'] == 5
    )
    tax_requirement_satisfied = taxes_configured or no_tax_allowed
    matias_company_id = row['matias_company_id']
    matias_company_id_configured = bool(
        matias_company_id and str(matias_company_id).strip()
    )

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
    if not customer_requested:
        missing.append('Solicita la activación de facturación electrónica desde la configuración fiscal')
    if not dev_flag_enabled:
        missing.append('Facturación electrónica pendiente de habilitación interna por WARO/Matias')
    if not fiscal_data_complete:
        missing.append(
            'Faltan datos fiscales: ' + ', '.join(missing_fiscal_fields)
        )
    if not active_resolution:
        missing.append('No hay una resolución DIAN vigente con numeración disponible')
    if not tax_requirement_satisfied:
        missing.append(
            'La configuración fiscal requiere INC/IVA activo o un escenario sin impuesto válido para emitir'
        )
    if not matias_company_id_configured:
        missing.append(
            'Falta UUID cliente Matias (client_uuid) para emitir con Matias'
        )

    return {
        'ready': (
            customer_requested
            and dev_flag_enabled
            and fiscal_data_complete
            and active_resolution
            and tax_requirement_satisfied
            and matias_company_id_configured
        ),
        'checks': {
            'customer_requested':   customer_requested,
            'dev_flag_enabled':     dev_flag_enabled,
            'fiscal_data_complete': fiscal_data_complete,
            'active_resolution':    active_resolution,
            'taxes_configured':     taxes_configured,
            'tax_requirement_satisfied': tax_requirement_satisfied,
            'matias_company_id_configured': matias_company_id_configured,
        },
        'missing': missing,
    }

"""
Invoice presentation helpers — print/email representation of electronic invoices.

Roles (do not mix):
  - issuer:              tenant fiscal identity (DIAN AccountingSupplierParty)
  - technology platform: WARO (software) — never the legal issuer
  - facturador:          technical DIAN channel (Matias Casa de Software today)

WARO = technology. Tenant = issuer. Matias = facturador técnico.
`matias_company_id` / client_uuid binds the tenant company in Matias; it is not
WARO's NIT.
"""
from app.services.invoicing_presentation.factory import build_invoice_presentation
from app.services.invoicing_presentation.public_error import public_invoice_error_message
from app.services.invoicing_presentation.resolve_issuer import (
    commercial_header_name,
    format_issuer_label,
    resolve_tenant_issuer,
)

__all__ = [
    "build_invoice_presentation",
    "commercial_header_name",
    "format_issuer_label",
    "public_invoice_error_message",
    "resolve_tenant_issuer",
]

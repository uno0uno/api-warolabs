"""
Matias facturador presentation builder.

Emisor = tenant fiscal only.
provider_meta documents Matias Casa de Software client_uuid (technical), not issuer.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.core.platform_legal import get_facturador_legal_entity
from app.services.invoicing_presentation.casting import (
    as_optional_int,
    clean_str,
    date_iso,
    keep_datetime_or_iso,
    row_get,
)
from app.services.invoicing_presentation.resolve_issuer import resolve_tenant_issuer


class MatiasPresentationBuilder:
    """Build print/email presentation for invoices emitted via Matias API."""

    facturador = "matias"

    def build(
        self,
        *,
        invoice_row: Any,
        order_row: Any,
        fiscal_row: Any,
        public_profile: Any = None,
        resolution_row: Any = None,
        tax_details: Optional[List[Dict[str, Any]]] = None,
        serialize_datetimes: bool = False,
    ) -> Dict[str, Any]:
        cufe = clean_str(row_get(invoice_row, "cufe"))
        prefix = clean_str(row_get(invoice_row, "prefix"))
        invoice_number = as_optional_int(row_get(invoice_row, "invoice_number"))
        status = clean_str(row_get(invoice_row, "status"))

        number: Optional[str] = None
        if prefix is not None and invoice_number is not None:
            number = f"{prefix}-{invoice_number}"
        elif prefix and row_get(invoice_row, "invoice_number") is not None:
            number = f"{prefix}-{row_get(invoice_row, 'invoice_number')}"

        issuer = resolve_tenant_issuer(fiscal_row)

        acquirer_name = (
            clean_str(row_get(order_row, "customer_fiscal_business_name"))
            or clean_str(row_get(order_row, "customer_name"))
            or "Consumidor final"
        )

        matias_uuid = clean_str(row_get(fiscal_row, "matias_company_id"))

        resolution = None
        res_source = resolution_row if resolution_row is not None else None
        res_number = clean_str(row_get(res_source, "resolution_number")) if res_source else None
        if not res_number:
            res_number = clean_str(row_get(invoice_row, "resolution_number"))
            if res_number:
                res_source = invoice_row

        if res_number:
            res_prefix = clean_str(row_get(res_source, "prefix")) or prefix
            resolution = {
                "number": res_number,
                "prefix": res_prefix,
                "from_number": as_optional_int(row_get(res_source, "from_number")),
                "to_number": as_optional_int(row_get(res_source, "to_number")),
                "date_from": date_iso(row_get(res_source, "date_from")),
                "date_to": date_iso(row_get(res_source, "date_to")),
            }

        attachments = {
            "pdf": bool(row_get(invoice_row, "r2_pdf_key")),
            "xml": bool(row_get(invoice_row, "r2_xml_key")),
        }

        return {
            "number": number,
            "prefix": prefix,
            "invoice_number": invoice_number,
            "cufe": cufe,
            "status": status,
            "emitted_at": keep_datetime_or_iso(
                row_get(invoice_row, "emitted_at"),
                serialize=serialize_datetimes,
            ),
            "created_at": keep_datetime_or_iso(
                row_get(invoice_row, "created_at"),
                serialize=serialize_datetimes,
            ),
            "dian_url": (
                f"https://catalogo-vpfe.dian.gov.co/document/searchqr?documentkey={cufe}"
                if cufe
                else None
            ),
            "issuer": issuer,
            "acquirer": {
                "name": acquirer_name,
                "fiscal_id_type": clean_str(row_get(order_row, "customer_fiscal_id_type")),
                "fiscal_id": clean_str(row_get(order_row, "customer_fiscal_id")),
                "email": clean_str(row_get(order_row, "customer_fiscal_email"))
                or clean_str(row_get(order_row, "customer_email")),
            },
            "payment": {
                "method": clean_str(row_get(order_row, "payment_method")),
                "status": clean_str(row_get(order_row, "payment_status")),
            },
            "resolution": resolution,
            "tax_details": list(tax_details or []),
            "attachments": attachments,
            "provider_meta": {
                "facturador": self.facturador,
                "technology_platform": "waro",
                "matias_client_uuid": matias_uuid,
                # Public legal label from env (FACTURADOR_LEGAL_*), not PAT secrets
                "facturador_legal": {
                    k: v
                    for k, v in get_facturador_legal_entity().items()
                    if k in (
                        "brand_name",
                        "legal_name",
                        "nit",
                        "role_label",
                        "not_issuer_disclaimer",
                        "slug",
                    )
                    and v
                },
            },
        }

"""
Resolve DIAN issuer party from tenant fiscal data only.

Never falls back to product brand ("WARO") or marketing display_name as issuer.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.services.invoicing_presentation.casting import clean_str, row_get


def resolve_tenant_issuer(fiscal_row: Any) -> Dict[str, Optional[str]]:
    """
    Build issuer dict from tenant_fiscal_data (or a joined row with fiscal cols).

    Accepts either:
      - columns: business_name, nit, fiscal_address, city, phone, email
      - aliases: fiscal_business_name (used in some JOIN SELECTs)

    Returns all keys; values None when missing. Empty issuer if no name and no nit.
    """
    name = clean_str(row_get(fiscal_row, "business_name")) or clean_str(
        row_get(fiscal_row, "fiscal_business_name")
    )
    nit = clean_str(row_get(fiscal_row, "nit"))
    address = clean_str(row_get(fiscal_row, "fiscal_address"))
    city = clean_str(row_get(fiscal_row, "fiscal_city")) or clean_str(
        row_get(fiscal_row, "city")
    )
    phone = clean_str(row_get(fiscal_row, "fiscal_phone")) or clean_str(
        row_get(fiscal_row, "phone")
    )
    email = clean_str(row_get(fiscal_row, "fiscal_email")) or clean_str(
        row_get(fiscal_row, "email")
    )

    # Do not invent issuer from public profile / brand.
    if not name and not nit:
        return {
            "name": None,
            "fiscal_id_type": None,
            "fiscal_id": None,
            "address": None,
            "city": None,
            "phone": None,
            "email": None,
        }

    return {
        "name": name,
        "fiscal_id_type": "NIT" if nit else None,
        "fiscal_id": nit,
        "address": address,
        "city": city,
        "phone": phone,
        "email": email,
    }


def format_issuer_label(issuer: Optional[Dict[str, Any]]) -> Optional[str]:
    """Single-line label for thermal tickets: 'Razón social - NIT xxx'."""
    if not issuer:
        return None
    name = clean_str(issuer.get("name"))
    nit = clean_str(issuer.get("fiscal_id"))
    if name and nit:
        return f"{name} - NIT {nit}"
    return name or (f"NIT {nit}" if nit else None)


def commercial_header_name(
    *,
    fiscal_row: Any = None,
    public_profile: Any = None,
    prefer_fiscal: bool = False,
) -> Optional[str]:
    """
    Commercial receipt header (not the legal Emisor block).

    - prefer_fiscal=True: FE email subject / FE-oriented sends
    - prefer_fiscal=False: POS brand display_name first, then fiscal name
    Never returns the product brand hardcoded string.
    """
    fiscal_name = clean_str(row_get(fiscal_row, "business_name")) or clean_str(
        row_get(fiscal_row, "fiscal_business_name")
    )
    display = clean_str(row_get(public_profile, "display_name"))
    if prefer_fiscal:
        return fiscal_name or display
    return display or fiscal_name

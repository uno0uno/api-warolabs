"""
Platform legal identities for receipt/print footers.

Source of truth: environment via app.config.settings (never hardcode secrets/PII in front).

Roles on printed documents:
  - Emisor / vendedor: TENANT fiscal data (DB) — independent per restaurant
  - WARO: POS software / technology platform (WARO_LEGAL_*)
  - Facturador: Matias API / LOPEZSOFT (FACTURADOR_LEGAL_*) — technical FE channel only

Matias legal identity (public terms https://matias-api.com/terminos/):
  LOPEZSOFT S.A.S., NIT 901.091.403-2, brand MATIAS API.
  Set via env in production; do not confuse with MATIAS_TOKEN_* PATs.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.config import settings


def _s(value: Optional[str]) -> str:
    return (value or "").strip()


def get_waro_legal_entity() -> Dict[str, Any]:
    """Full WARO legal block from env (may include PII — not for browser by default)."""
    phones = tuple(
        p for p in (_s(settings.waro_legal_phone_1), _s(settings.waro_legal_phone_2)) if p
    )
    return {
        "commercial_name": _s(settings.waro_legal_commercial_name),
        "legal_name": _s(settings.waro_legal_legal_name),
        "document_type": _s(settings.waro_legal_document_type),
        "document_number": _s(settings.waro_legal_document_number),
        "nit": _s(settings.waro_legal_nit),
        "address": _s(settings.waro_legal_address),
        "city": _s(settings.waro_legal_city),
        "email": _s(settings.waro_legal_email),
        "phones": phones,
        "iva_responsibility_label": _s(settings.waro_legal_iva_label),
        "role_label": _s(settings.waro_legal_role_label) or "Proveedor tecnológico / software",
        "not_issuer_disclaimer": _s(settings.waro_legal_not_issuer_disclaimer)
        or "No es el emisor de esta venta",
    }


def get_facturador_legal_entity() -> Dict[str, Any]:
    """
    Facturador técnico (Matias / LOPEZSOFT) from env.

    Print uses brand + optional NIT/legal_name. Never includes MATIAS_TOKEN_*.
    """
    return {
        "brand_name": _s(settings.facturador_legal_brand_name),
        "legal_name": _s(settings.facturador_legal_legal_name),
        "nit": _s(settings.facturador_legal_nit),
        "role_label": _s(settings.facturador_legal_role_label) or "Facturador técnico DIAN",
        "not_issuer_disclaimer": _s(settings.facturador_legal_not_issuer_disclaimer)
        or "No es el emisor de esta venta",
        "city": _s(settings.facturador_legal_city),
        "support_email": _s(settings.facturador_legal_support_email),
        "slug": "matias",
    }


def get_platform_legal_for_print() -> Dict[str, Any]:
    """
    Safe payload for POS print (front). No document numbers, personal emails, phones.
    """
    waro = get_waro_legal_entity()
    facturador = get_facturador_legal_entity()
    return {
        "software": {
            "role_label": waro["role_label"],
            "commercial_name": waro["commercial_name"] or None,
            "legal_name": waro["legal_name"] or None,
            "nit": waro["nit"] or None,
            "iva_responsibility_label": waro["iva_responsibility_label"] or None,
            "not_issuer_disclaimer": waro["not_issuer_disclaimer"],
        },
        "facturador": {
            "role_label": facturador["role_label"],
            "brand_name": facturador["brand_name"] or None,
            "legal_name": facturador["legal_name"] or None,
            "nit": facturador["nit"] or None,
            "not_issuer_disclaimer": facturador["not_issuer_disclaimer"],
            "slug": facturador["slug"],
        },
    }


def waro_platform_footer_lines(*, with_fe_note: bool = False) -> List[str]:
    """Compact footer lines for email/text receipts."""
    waro = get_waro_legal_entity()
    facturador = get_facturador_legal_entity()
    lines: List[str] = ["--------------------------------"]

    if waro["commercial_name"] or waro["nit"]:
        lines.append(waro["role_label"])
        if waro["commercial_name"] and waro["nit"]:
            lines.append(f"{waro['commercial_name']} — NIT {waro['nit']}")
        elif waro["commercial_name"]:
            lines.append(waro["commercial_name"])
        elif waro["nit"]:
            lines.append(f"NIT {waro['nit']}")
        if waro["legal_name"]:
            lines.append(waro["legal_name"])
        if waro["iva_responsibility_label"]:
            lines.append(waro["iva_responsibility_label"])
        lines.append(waro["not_issuer_disclaimer"])

    if with_fe_note:
        brand = facturador["brand_name"] or "Matias API"
        if facturador["nit"]:
            lines.append(
                f"{facturador['role_label']}: {brand} — NIT {facturador['nit']}"
            )
        else:
            lines.append(f"{facturador['role_label']}: {brand}")
        if facturador["legal_name"]:
            lines.append(facturador["legal_name"])
        lines.append(facturador["not_issuer_disclaimer"])
        lines.append("Emisor DIAN: establecimiento (tenant)")
    else:
        lines.append("Comprobante del establecimiento (software WARO)")

    return lines


def waro_platform_footer_text(*, with_fe_note: bool = False) -> str:
    return "\n".join(waro_platform_footer_lines(with_fe_note=with_fe_note))


# Back-compat alias for existing imports
def get_waro_legal_entity_legacy_alias():
    return get_waro_legal_entity()

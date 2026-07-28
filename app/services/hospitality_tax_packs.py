"""Hospitality tax pack seeds — wave-1 (#1847) + wave-2 (#1862/#1863).

Wave-1 rates match front_nuxt/composables/useTenantTaxProfile.ts WAVE1_TAX_PRESETS.
Wave-2 simple (#1862) and multi-rate DE/NL (#1863): warocol.com#1860 epic research.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Mapping, Optional

WAVE1_COUNTRY_CODES = frozenset({"PA", "CL", "DO", "UY", "AU", "NZ", "SG", "AE"})

WAVE2_SIMPLE_COUNTRY_CODES = frozenset(
    {"PE", "MX", "CR", "AR", "ES", "FR", "GB", "CN"}
)

WAVE2_MULTI_COUNTRY_CODES = frozenset({"DE", "NL"})

WAVE1_TAX_PACKS: Dict[str, Dict[str, Any]] = {
    "PA": {
        "tax_lines": [
            {
                "key": "itbms",
                "label": "ITBMS 7%",
                "rate": 0.07,
                "included_in_price": False,
                "gl_role": "iva",
            }
        ],
        "category_map": {"standard": "itbms", "liquor": "itbms", "exempt": None},
    },
    "CL": {
        "tax_lines": [
            {
                "key": "iva",
                "label": "IVA 19%",
                "rate": 0.19,
                "included_in_price": False,
                "gl_role": "iva",
            }
        ],
        "category_map": {"standard": "iva", "liquor": "iva", "exempt": None},
    },
    "DO": {
        "tax_lines": [
            {
                "key": "itbis",
                "label": "ITBIS 18%",
                "rate": 0.18,
                "included_in_price": False,
                "gl_role": "iva",
            }
        ],
        "category_map": {"standard": "itbis", "liquor": "itbis", "exempt": None},
    },
    "UY": {
        "tax_lines": [
            {
                "key": "iva",
                "label": "IVA 22%",
                "rate": 0.22,
                "included_in_price": False,
                "gl_role": "iva",
            }
        ],
        "category_map": {"standard": "iva", "liquor": "iva", "exempt": None},
    },
    "AU": {
        "tax_lines": [
            {
                "key": "gst",
                "label": "GST 10%",
                "rate": 0.10,
                "included_in_price": True,
                "gl_role": "iva",
            }
        ],
        "category_map": {"standard": "gst", "liquor": "gst", "exempt": None},
    },
    "NZ": {
        "tax_lines": [
            {
                "key": "gst",
                "label": "GST 15%",
                "rate": 0.15,
                "included_in_price": True,
                "gl_role": "iva",
            }
        ],
        "category_map": {"standard": "gst", "liquor": "gst", "exempt": None},
    },
    "SG": {
        "tax_lines": [
            {
                "key": "gst",
                "label": "GST 9%",
                "rate": 0.09,
                "included_in_price": True,
                "gl_role": "iva",
            }
        ],
        "category_map": {"standard": "gst", "liquor": "gst", "exempt": None},
    },
    "AE": {
        "tax_lines": [
            {
                "key": "vat",
                "label": "VAT 5%",
                "rate": 0.05,
                "included_in_price": False,
                "gl_role": "iva",
            }
        ],
        "category_map": {"standard": "vat", "liquor": "vat", "exempt": None},
    },
}

WAVE2_SIMPLE_TAX_PACKS: Dict[str, Dict[str, Any]] = {
    "PE": {
        "tax_lines": [
            {
                "key": "igv",
                "label": "IGV 18%",
                "rate": 0.18,
                "included_in_price": False,
                "gl_role": "iva",
            }
        ],
        "category_map": {"standard": "igv", "liquor": "igv", "exempt": None},
    },
    "MX": {
        "tax_lines": [
            {
                "key": "iva",
                "label": "IVA 16%",
                "rate": 0.16,
                "included_in_price": False,
                "gl_role": "iva",
            }
        ],
        "category_map": {"standard": "iva", "liquor": "iva", "exempt": None},
    },
    "CR": {
        "tax_lines": [
            {
                "key": "iva",
                "label": "IVA 13%",
                "rate": 0.13,
                "included_in_price": False,
                "gl_role": "iva",
            }
        ],
        "category_map": {"standard": "iva", "liquor": "iva", "exempt": None},
    },
    "AR": {
        "tax_lines": [
            {
                "key": "iva",
                "label": "IVA 21%",
                "rate": 0.21,
                "included_in_price": False,
                "gl_role": "iva",
            }
        ],
        "category_map": {"standard": "iva", "liquor": "iva", "exempt": None},
    },
    "ES": {
        "tax_lines": [
            {
                "key": "iva",
                "label": "IVA 10%",
                "rate": 0.10,
                "included_in_price": False,
                "gl_role": "iva",
            }
        ],
        "category_map": {"standard": "iva", "liquor": "iva", "exempt": None},
    },
    "FR": {
        "tax_lines": [
            {
                "key": "tva",
                "label": "TVA 10%",
                "rate": 0.10,
                "included_in_price": False,
                "gl_role": "iva",
            }
        ],
        "category_map": {"standard": "tva", "liquor": "tva", "exempt": None},
    },
    "GB": {
        "tax_lines": [
            {
                "key": "vat",
                "label": "VAT 20%",
                "rate": 0.20,
                "included_in_price": False,
                "gl_role": "iva",
            }
        ],
        "category_map": {"standard": "vat", "liquor": "vat", "exempt": None},
    },
    "CN": {
        "tax_lines": [
            {
                "key": "vat",
                "label": "VAT 6%",
                "rate": 0.06,
                "included_in_price": False,
                "gl_role": "iva",
            }
        ],
        "category_map": {"standard": "vat", "liquor": "vat", "exempt": None},
    },
}


WAVE2_MULTI_TAX_PACKS: Dict[str, Dict[str, Any]] = {
    # Commercial approx: food/soft → standard (reduced); alcohol → liquor (full).
    "DE": {
        "tax_lines": [
            {
                "key": "mwst_reduced",
                "label": "MwSt 7%",
                "rate": 0.07,
                "included_in_price": False,
                "gl_role": "iva",
            },
            {
                "key": "mwst_standard",
                "label": "MwSt 19%",
                "rate": 0.19,
                "included_in_price": False,
                "gl_role": "iva",
            },
        ],
        "category_map": {
            "standard": "mwst_reduced",
            "liquor": "mwst_standard",
            "exempt": None,
        },
    },
    "NL": {
        "tax_lines": [
            {
                "key": "btw_reduced",
                "label": "BTW 9%",
                "rate": 0.09,
                "included_in_price": False,
                "gl_role": "iva",
            },
            {
                "key": "btw_standard",
                "label": "BTW 21%",
                "rate": 0.21,
                "included_in_price": False,
                "gl_role": "iva",
            },
        ],
        "category_map": {
            "standard": "btw_reduced",
            "liquor": "btw_standard",
            "exempt": None,
        },
    },
}

# Union for seed lookup (wave-1 + wave-2 simple + DE/NL multi).
COUNTRY_TAX_PACKS: Dict[str, Dict[str, Any]] = {
    **WAVE1_TAX_PACKS,
    **WAVE2_SIMPLE_TAX_PACKS,
    **WAVE2_MULTI_TAX_PACKS,
}
SEEDED_COUNTRY_CODES = frozenset(COUNTRY_TAX_PACKS)


def _copy_pack(pack: Mapping[str, Any]) -> Dict[str, Any]:
    """Shallow-copy pack for callers (1-rate or multi-rate)."""
    return {
        "tax_lines": list(pack["tax_lines"]),
        "category_map": dict(pack["category_map"]),
    }


def pack_for_country(country_code: str) -> Optional[Dict[str, Any]]:
    """Return seeded country pack, or None for CO / jurisdiction / unknown."""
    code = str(country_code or "").upper()
    if code == "CO" or code not in SEEDED_COUNTRY_CODES:
        return None
    pack = COUNTRY_TAX_PACKS.get(code)
    return _copy_pack(pack) if pack else None


def wave1_pack_for_country(country_code: str) -> Optional[Dict[str, Any]]:
    """Wave-1-only lookup (tests / callers that must not see wave-2)."""
    code = str(country_code or "").upper()
    if code == "CO" or code not in WAVE1_COUNTRY_CODES:
        return None
    pack = WAVE1_TAX_PACKS.get(code)
    return _copy_pack(pack) if pack else None


def tax_config_from_wave1_pack(pack: Mapping[str, Any]) -> Dict[str, Any]:
    """Build a tenant_tax_config-shaped dict for the hospitality tax engine."""
    return {
        "inc_applicable": False,
        "iva_applicable": False,
        "liquor_tax_applicable": False,
        "commercial_tax_applicable": True,
        "tax_lines": list(pack["tax_lines"]),
        "category_map": dict(pack["category_map"]),
    }


async def ensure_country_tax_pack(conn, tenant_id, country_code: str) -> bool:
    """Write seeded pack when tax_lines is unset. Never overwrites existing lines."""
    pack = pack_for_country(country_code)
    if not pack:
        return False

    row = await conn.fetchrow(
        "SELECT tax_lines FROM tenant_tax_config WHERE tenant_id = $1",
        tenant_id,
    )
    if row is None:
        await conn.execute(
            "INSERT INTO tenant_tax_config (tenant_id) VALUES ($1) ON CONFLICT DO NOTHING",
            tenant_id,
        )
        row = await conn.fetchrow(
            "SELECT tax_lines FROM tenant_tax_config WHERE tenant_id = $1",
            tenant_id,
        )
    if row is None or row["tax_lines"] is not None:
        return False

    result = await conn.execute(
        """
        UPDATE tenant_tax_config
        SET tax_lines = $2::jsonb,
            category_map = $3::jsonb,
            commercial_tax_applicable = true,
            updated_at = NOW()
        WHERE tenant_id = $1
          AND tax_lines IS NULL
        """,
        tenant_id,
        json.dumps(pack["tax_lines"]),
        json.dumps(pack["category_map"]),
    )
    return result.endswith("1")


async def ensure_wave1_tax_pack(conn, tenant_id, country_code: str) -> bool:
    """Alias kept for existing call sites (onboarding + tax-config GET)."""
    return await ensure_country_tax_pack(conn, tenant_id, country_code)

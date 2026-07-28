"""Wave-1 hospitality tax pack seeds — warocol.com#1847.

Rates match front_nuxt/composables/useTenantTaxProfile.ts WAVE1_TAX_PRESETS.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Mapping, Optional

WAVE1_COUNTRY_CODES = frozenset({"PA", "CL", "DO", "UY", "AU", "NZ", "SG", "AE"})

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


def wave1_pack_for_country(country_code: str) -> Optional[Dict[str, Any]]:
    code = str(country_code or "").upper()
    if code == "CO" or code not in WAVE1_COUNTRY_CODES:
        return None
    return WAVE1_TAX_PACKS.get(code)


def tax_config_from_wave1_pack(pack: Mapping[str, Any]) -> Dict[str, Any]:
    """Build a tenant_tax_config-shaped dict for the hospitality tax engine."""
    return {
        "inc_applicable": False,
        "iva_applicable": False,
        "liquor_tax_applicable": False,
        "tax_lines": list(pack["tax_lines"]),
        "category_map": dict(pack["category_map"]),
    }


async def ensure_wave1_tax_pack(conn, tenant_id, country_code: str) -> bool:
    """Write wave-1 pack when tax_lines is unset. Never overwrites existing lines."""
    pack = wave1_pack_for_country(country_code)
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
            updated_at = NOW()
        WHERE tenant_id = $1
          AND tax_lines IS NULL
        """,
        tenant_id,
        json.dumps(pack["tax_lines"]),
        json.dumps(pack["category_map"]),
    )
    return result.endswith("1")

"""US/CA hospitality tax jurisdiction seeds — warocol.com#1848.

Static reference defaults (state/province only). Not legal advice.
City/local meal taxes are out of v1.

CA GST+PST/QST provinces use a combined effective rate line so the existing
hospitality_tax_engine (one line per category) can compute POS/cierre.
Regime metadata distinguishes HST vs GST vs GST+PST for AC/tests.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional, Tuple

JURISDICTION_COUNTRIES = frozenset({"US", "CA"})


def _line(
    key: str,
    label: str,
    rate: float,
    *,
    included_in_price: bool = False,
    gl_role: str = "iva",
) -> Dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "rate": rate,
        "included_in_price": included_in_price,
        "gl_role": gl_role,
    }


def _pack(
    *,
    code: str,
    label: str,
    regime: str,
    lines: List[Dict[str, Any]],
    map_key: Optional[str] = None,
    components: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    primary = map_key or (lines[0]["key"] if lines else None)
    return {
        "code": code,
        "label": label,
        "regime": regime,
        "tax_lines": lines,
        "components": list(components or []),
        "category_map": {
            "standard": primary,
            "liquor": primary,
            "exempt": None,
        },
    }


# Representative combined state-level defaults for hospitality (v1).
# Rates intentionally differ across states so AC (TX ≠ OR) is testable.
US_STATE_TAX_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "AL": _pack(code="AL", label="Alabama", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 9%", 0.09)]),
    "AK": _pack(code="AK", label="Alaska", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 0%", 0.0)]),
    "AZ": _pack(code="AZ", label="Arizona", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 8.1%", 0.081)]),
    "AR": _pack(code="AR", label="Arkansas", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 9.5%", 0.095)]),
    "CA": _pack(code="CA", label="California", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 8.7%", 0.087)]),
    "CO": _pack(code="CO", label="Colorado", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 7.8%", 0.078)]),
    "CT": _pack(code="CT", label="Connecticut", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 6.35%", 0.0635)]),
    "DE": _pack(code="DE", label="Delaware", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 0%", 0.0)]),
    "DC": _pack(code="DC", label="District of Columbia", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 10%", 0.10)]),
    "FL": _pack(code="FL", label="Florida", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 7%", 0.07)]),
    "GA": _pack(code="GA", label="Georgia", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 7.3%", 0.073)]),
    "HI": _pack(code="HI", label="Hawaii", regime="sales_tax", lines=[_line("sales_tax", "GET 4.5%", 0.045)]),
    "ID": _pack(code="ID", label="Idaho", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 6%", 0.06)]),
    "IL": _pack(code="IL", label="Illinois", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 8.8%", 0.088)]),
    "IN": _pack(code="IN", label="Indiana", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 7%", 0.07)]),
    "IA": _pack(code="IA", label="Iowa", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 7%", 0.07)]),
    "KS": _pack(code="KS", label="Kansas", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 8.7%", 0.087)]),
    "KY": _pack(code="KY", label="Kentucky", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 6%", 0.06)]),
    "LA": _pack(code="LA", label="Louisiana", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 9.5%", 0.095)]),
    "ME": _pack(code="ME", label="Maine", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 5.5%", 0.055)]),
    "MD": _pack(code="MD", label="Maryland", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 6%", 0.06)]),
    "MA": _pack(code="MA", label="Massachusetts", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 6.25%", 0.0625)]),
    "MI": _pack(code="MI", label="Michigan", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 6%", 0.06)]),
    "MN": _pack(code="MN", label="Minnesota", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 7.4%", 0.074)]),
    "MS": _pack(code="MS", label="Mississippi", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 7%", 0.07)]),
    "MO": _pack(code="MO", label="Missouri", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 8.3%", 0.083)]),
    "MT": _pack(code="MT", label="Montana", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 0%", 0.0)]),
    "NE": _pack(code="NE", label="Nebraska", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 6.9%", 0.069)]),
    "NV": _pack(code="NV", label="Nevada", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 8.2%", 0.082)]),
    "NH": _pack(code="NH", label="New Hampshire", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 0%", 0.0)]),
    "NJ": _pack(code="NJ", label="New Jersey", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 6.625%", 0.06625)]),
    "NM": _pack(code="NM", label="New Mexico", regime="sales_tax", lines=[_line("sales_tax", "GRT 7.8%", 0.078)]),
    "NY": _pack(code="NY", label="New York", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 8.5%", 0.085)]),
    "NC": _pack(code="NC", label="North Carolina", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 7%", 0.07)]),
    "ND": _pack(code="ND", label="North Dakota", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 7%", 0.07)]),
    "OH": _pack(code="OH", label="Ohio", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 7.2%", 0.072)]),
    "OK": _pack(code="OK", label="Oklahoma", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 8.9%", 0.089)]),
    "OR": _pack(code="OR", label="Oregon", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 0%", 0.0)]),
    "PA": _pack(code="PA", label="Pennsylvania", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 6%", 0.06)]),
    "RI": _pack(code="RI", label="Rhode Island", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 7%", 0.07)]),
    "SC": _pack(code="SC", label="South Carolina", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 7.5%", 0.075)]),
    "SD": _pack(code="SD", label="South Dakota", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 6.4%", 0.064)]),
    "TN": _pack(code="TN", label="Tennessee", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 9.5%", 0.095)]),
    "TX": _pack(code="TX", label="Texas", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 8.25%", 0.0825)]),
    "UT": _pack(code="UT", label="Utah", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 7.2%", 0.072)]),
    "VT": _pack(code="VT", label="Vermont", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 6.4%", 0.064)]),
    "VA": _pack(code="VA", label="Virginia", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 5.8%", 0.058)]),
    "WA": _pack(code="WA", label="Washington", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 9.4%", 0.094)]),
    "WV": _pack(code="WV", label="West Virginia", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 6.5%", 0.065)]),
    "WI": _pack(code="WI", label="Wisconsin", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 5.5%", 0.055)]),
    "WY": _pack(code="WY", label="Wyoming", regime="sales_tax", lines=[_line("sales_tax", "Sales tax 5.4%", 0.054)]),
}


def _ca_hst(code: str, label: str, rate: float) -> Dict[str, Any]:
    pct = round(rate * 100)
    return _pack(
        code=code,
        label=label,
        regime="hst",
        lines=[_line("hst", f"HST {pct}%", rate)],
    )


def _ca_gst(code: str, label: str) -> Dict[str, Any]:
    return _pack(
        code=code,
        label=label,
        regime="gst",
        lines=[_line("gst", "GST 5%", 0.05)],
    )


def _ca_gst_pst(
    code: str,
    label: str,
    pst_rate: float,
    *,
    pst_key: str = "pst",
    pst_name: str = "PST",
) -> Dict[str, Any]:
    """Combined effective GST+PST/QST for single-line engine mapping."""
    combined = round(0.05 + pst_rate, 6)
    pst_pct = round(pst_rate * 1000) / 10
    combined_pct = round(combined * 1000) / 10
    components = [
        _line("gst", "GST 5%", 0.05),
        _line(pst_key, f"{pst_name} {pst_pct}%", pst_rate),
    ]
    return _pack(
        code=code,
        label=label,
        regime="gst_pst",
        lines=[
            _line(
                "gst_pst",
                f"GST+{pst_name} {combined_pct}%",
                combined,
            )
        ],
        map_key="gst_pst",
        components=components,
    )


CA_PROVINCE_TAX_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "ON": _ca_hst("ON", "Ontario", 0.13),
    "NB": _ca_hst("NB", "New Brunswick", 0.15),
    "NL": _ca_hst("NL", "Newfoundland and Labrador", 0.15),
    "NS": _ca_hst("NS", "Nova Scotia", 0.15),
    "PE": _ca_hst("PE", "Prince Edward Island", 0.15),
    "AB": _ca_gst("AB", "Alberta"),
    "YT": _ca_gst("YT", "Yukon"),
    "NT": _ca_gst("NT", "Northwest Territories"),
    "NU": _ca_gst("NU", "Nunavut"),
    "BC": _ca_gst_pst("BC", "British Columbia", 0.07),
    "SK": _ca_gst_pst("SK", "Saskatchewan", 0.06),
    "MB": _ca_gst_pst("MB", "Manitoba", 0.07),
    "QC": _ca_gst_pst("QC", "Quebec", 0.09975, pst_key="qst", pst_name="QST"),
}


def jurisdiction_pack(country_code: str, jurisdiction_code: str) -> Optional[Dict[str, Any]]:
    country = str(country_code or "").upper()
    code = str(jurisdiction_code or "").upper()
    if not code:
        return None
    if country == "US":
        return US_STATE_TAX_DEFAULTS.get(code)
    if country == "CA":
        return CA_PROVINCE_TAX_DEFAULTS.get(code)
    return None


def list_jurisdictions(country_code: str) -> List[Dict[str, Any]]:
    country = str(country_code or "").upper()
    source = US_STATE_TAX_DEFAULTS if country == "US" else (
        CA_PROVINCE_TAX_DEFAULTS if country == "CA" else {}
    )
    out: List[Dict[str, Any]] = []
    for pack in source.values():
        primary_key = pack["category_map"].get("standard")
        primary = next(
            (line for line in pack["tax_lines"] if line["key"] == primary_key),
            pack["tax_lines"][0] if pack["tax_lines"] else None,
        )
        out.append(
            {
                "code": pack["code"],
                "label": pack["label"],
                "regime": pack["regime"],
                "rate": primary["rate"] if primary else 0,
                "lines": list(pack["tax_lines"]),
                "components": list(pack.get("components") or []),
            }
        )
    return sorted(out, key=lambda item: item["code"])


def tax_config_from_jurisdiction_pack(pack: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "inc_applicable": False,
        "iva_applicable": False,
        "liquor_tax_applicable": False,
        "tax_lines": list(pack["tax_lines"]),
        "category_map": dict(pack["category_map"]),
        "tax_jurisdiction_code": pack.get("code"),
    }


def normalize_jurisdiction_code(
    country_code: str, jurisdiction_code: Optional[str]
) -> Optional[str]:
    if jurisdiction_code is None or str(jurisdiction_code).strip() == "":
        return None
    code = str(jurisdiction_code).strip().upper()
    pack = jurisdiction_pack(country_code, code)
    if not pack:
        raise ValueError(f"Unsupported jurisdiction {code} for {country_code}")
    return code


async def apply_jurisdiction_pack(
    conn,
    tenant_id,
    country_code: str,
    jurisdiction_code: str,
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Write jurisdiction + tax_lines/category_map from static pack.

    Always updates on explicit jurisdiction save (user intent).
    """
    pack = jurisdiction_pack(country_code, jurisdiction_code)
    if not pack:
        return False, None

    await conn.execute(
        "INSERT INTO tenant_tax_config (tenant_id) VALUES ($1) ON CONFLICT DO NOTHING",
        tenant_id,
    )
    row = await conn.fetchrow(
        """
        UPDATE tenant_tax_config
        SET tax_jurisdiction_code = $2,
            tax_lines = $3::jsonb,
            category_map = $4::jsonb,
            commercial_tax_applicable = true,
            updated_at = NOW()
        WHERE tenant_id = $1
        RETURNING *
        """,
        tenant_id,
        pack["code"],
        json.dumps(pack["tax_lines"]),
        json.dumps(pack["category_map"]),
    )
    return True, dict(row) if row else None

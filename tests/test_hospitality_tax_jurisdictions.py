"""US/CA hospitality tax jurisdictions — warocol.com#1848."""
from pathlib import Path
from uuid import uuid4

import pytest

from app.services.hospitality_tax_engine import (
    compute_category_breakdown,
    resolve_tax_profile,
)
from app.services.hospitality_tax_jurisdictions import (
    CA_PROVINCE_TAX_DEFAULTS,
    JURISDICTION_COUNTRIES,
    US_STATE_TAX_DEFAULTS,
    apply_jurisdiction_pack,
    jurisdiction_pack,
    list_jurisdictions,
    normalize_jurisdiction_code,
    tax_config_from_jurisdiction_pack,
)


def test_us_catalog_has_50_states_plus_dc():
    assert len(US_STATE_TAX_DEFAULTS) == 51
    assert "TX" in US_STATE_TAX_DEFAULTS
    assert "OR" in US_STATE_TAX_DEFAULTS
    assert "DC" in US_STATE_TAX_DEFAULTS


def test_ca_catalog_has_13_provinces():
    assert len(CA_PROVINCE_TAX_DEFAULTS) == 13
    assert CA_PROVINCE_TAX_DEFAULTS["ON"]["regime"] == "hst"
    assert CA_PROVINCE_TAX_DEFAULTS["AB"]["regime"] == "gst"
    assert CA_PROVINCE_TAX_DEFAULTS["BC"]["regime"] == "gst_pst"
    assert CA_PROVINCE_TAX_DEFAULTS["QC"]["regime"] == "gst_pst"


def test_two_us_states_yield_different_rates_same_category():
    tx = tax_config_from_jurisdiction_pack(US_STATE_TAX_DEFAULTS["TX"])
    oregon = tax_config_from_jurisdiction_pack(US_STATE_TAX_DEFAULTS["OR"])
    rows = [{"tax_category": "standard", "subtotal": 10000}]
    tx_tax, _, _ = compute_category_breakdown(rows, tx)
    or_tax, _, _ = compute_category_breakdown(rows, oregon)
    assert tx_tax == 825.0
    assert or_tax == 0.0
    assert tx_tax != or_tax


def test_ca_province_regimes_switch_behavior():
    on = tax_config_from_jurisdiction_pack(CA_PROVINCE_TAX_DEFAULTS["ON"])
    ab = tax_config_from_jurisdiction_pack(CA_PROVINCE_TAX_DEFAULTS["AB"])
    bc = tax_config_from_jurisdiction_pack(CA_PROVINCE_TAX_DEFAULTS["BC"])
    rows = [{"tax_category": "standard", "subtotal": 10000}]
    on_tax, _, on_label = compute_category_breakdown(rows, on)
    ab_tax, _, ab_label = compute_category_breakdown(rows, ab)
    bc_tax, _, bc_label = compute_category_breakdown(rows, bc)
    assert on_tax == 1300.0
    assert ab_tax == 500.0
    assert bc_tax == 1200.0
    assert "HST" in on_label
    assert "GST" in ab_label and "PST" not in ab_label
    assert "GST+PST" in bc_label
    assert CA_PROVINCE_TAX_DEFAULTS["ON"]["regime"] != CA_PROVINCE_TAX_DEFAULTS["BC"]["regime"]
    assert resolve_tax_profile(bc).line_for_category("exempt") is None


def test_list_jurisdictions_and_normalize():
    us = list_jurisdictions("US")
    ca = list_jurisdictions("CA")
    assert len(us) == 51
    assert len(ca) == 13
    assert us[0]["code"] < us[-1]["code"]
    assert normalize_jurisdiction_code("US", " tx ") == "TX"
    with pytest.raises(ValueError):
        normalize_jurisdiction_code("US", "ZZ")
    assert jurisdiction_pack("PA", "TX") is None
    assert JURISDICTION_COUNTRIES == frozenset({"US", "CA"})


def test_migration_is_add_only():
    sql = Path("migrations/119_tax_jurisdiction.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS tax_jurisdiction_code" in sql
    assert "DROP COLUMN" not in sql.upper()
    assert "DROP TABLE" not in sql.upper()


class _JurisConn:
    def __init__(self):
        self.row = {"tax_lines": None, "tax_jurisdiction_code": None}
        self.queries = []

    async def execute(self, query, *args):
        self.queries.append((query, args))
        return "INSERT 0 1"

    async def fetchrow(self, query, *args):
        self.queries.append((query, args))
        if "UPDATE tenant_tax_config" in query:
            self.row = {
                "tenant_id": args[0],
                "tax_jurisdiction_code": args[1],
                "tax_lines": args[2],
                "category_map": args[3],
            }
            return self.row
        return self.row


@pytest.mark.asyncio
async def test_apply_jurisdiction_pack_writes_code_and_lines():
    conn = _JurisConn()
    applied, row = await apply_jurisdiction_pack(conn, uuid4(), "US", "TX")
    assert applied is True
    assert row["tax_jurisdiction_code"] == "TX"
    assert any("UPDATE tenant_tax_config" in q for q, _ in conn.queries)

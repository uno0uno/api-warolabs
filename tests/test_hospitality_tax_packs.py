"""Hospitality tax packs — wave-1 (#1847) + wave-2 simple/multi (#1862/#1863)."""
from uuid import uuid4

import pytest

from app.services.hospitality_tax_engine import (
    compute_category_breakdown,
    resolve_tax_profile,
    tax_amount_float,
)
from app.services.hospitality_tax_packs import (
    WAVE1_COUNTRY_CODES,
    WAVE1_TAX_PACKS,
    WAVE2_MULTI_COUNTRY_CODES,
    WAVE2_MULTI_TAX_PACKS,
    WAVE2_SIMPLE_COUNTRY_CODES,
    WAVE2_SIMPLE_TAX_PACKS,
    ensure_country_tax_pack,
    ensure_wave1_tax_pack,
    pack_for_country,
    tax_config_from_wave1_pack,
    wave1_pack_for_country,
)


@pytest.mark.parametrize("country", sorted(WAVE1_COUNTRY_CODES))
def test_wave1_pack_exists_for_each_shortlist_country(country):
    pack = wave1_pack_for_country(country)
    assert pack is not None
    assert pack == WAVE1_TAX_PACKS[country]
    assert len(pack["tax_lines"]) == 1
    assert pack["category_map"]["exempt"] is None


@pytest.mark.parametrize("country", sorted(WAVE2_SIMPLE_COUNTRY_CODES))
def test_wave2_simple_pack_exists_for_each_country(country):
    pack = pack_for_country(country)
    assert pack is not None
    assert pack == WAVE2_SIMPLE_TAX_PACKS[country]
    assert len(pack["tax_lines"]) == 1
    assert pack["category_map"]["exempt"] is None
    assert pack["category_map"]["standard"] == pack["tax_lines"][0]["key"]
    assert pack["category_map"]["liquor"] == pack["tax_lines"][0]["key"]
    # wave1-only helper must not surface wave-2
    assert wave1_pack_for_country(country) is None


def test_wave1_pack_skips_colombia_and_unknown():
    assert wave1_pack_for_country("CO") is None
    assert wave1_pack_for_country("US") is None
    assert wave1_pack_for_country("") is None
    assert pack_for_country("CO") is None
    assert pack_for_country("US") is None


@pytest.mark.parametrize(
    ("country", "subtotal", "expected_tax"),
    [
        ("PA", 10000, 700.0),
        ("CL", 10000, 1900.0),
        ("DO", 10000, 1800.0),
        ("UY", 10000, 2200.0),
        ("AU", 11000, 1000.0),
        ("NZ", 11500, 1500.0),
        ("SG", 10900, 900.0),
        ("AE", 10000, 500.0),
    ],
)
def test_wave1_pack_computes_standard_and_exempt(country, subtotal, expected_tax):
    cfg = tax_config_from_wave1_pack(WAVE1_TAX_PACKS[country])
    rows = [
        {"tax_category": "standard", "subtotal": subtotal},
        {"tax_category": "exempt", "subtotal": 3000},
    ]
    std, liq, _ = compute_category_breakdown(rows, cfg)
    assert std == expected_tax
    assert liq == 0.0
    assert resolve_tax_profile(cfg).line_for_category("exempt") is None


@pytest.mark.parametrize(
    ("country", "subtotal", "expected_tax"),
    [
        ("PE", 10000, 1800.0),
        ("MX", 10000, 1600.0),
        ("CR", 10000, 1300.0),
        ("AR", 10000, 2100.0),
        ("ES", 10000, 1000.0),
        ("FR", 10000, 1000.0),
        ("GB", 10000, 2000.0),
        ("CN", 10000, 600.0),
    ],
)
def test_wave2_simple_pack_computes_standard_and_exempt(country, subtotal, expected_tax):
    cfg = tax_config_from_wave1_pack(WAVE2_SIMPLE_TAX_PACKS[country])
    rows = [
        {"tax_category": "standard", "subtotal": subtotal},
        {"tax_category": "exempt", "subtotal": 3000},
    ]
    std, liq, _ = compute_category_breakdown(rows, cfg)
    assert std == expected_tax
    assert liq == 0.0
    assert resolve_tax_profile(cfg).line_for_category("exempt") is None




@pytest.mark.parametrize("country", sorted(WAVE2_MULTI_COUNTRY_CODES))
def test_wave2_multi_pack_exists_for_each_country(country):
    pack = pack_for_country(country)
    assert pack is not None
    assert pack == WAVE2_MULTI_TAX_PACKS[country]
    assert len(pack["tax_lines"]) == 2
    assert pack["category_map"]["exempt"] is None
    assert pack["category_map"]["standard"] != pack["category_map"]["liquor"]
    assert wave1_pack_for_country(country) is None


@pytest.mark.parametrize(
    ("country", "std_subtotal", "liq_subtotal", "expected_std", "expected_liq"),
    [
        ("DE", 10000, 10000, 700.0, 1900.0),
        ("NL", 10000, 10000, 900.0, 2100.0),
    ],
)
def test_wave2_multi_pack_computes_standard_and_liquor(
    country, std_subtotal, liq_subtotal, expected_std, expected_liq
):
    cfg = tax_config_from_wave1_pack(WAVE2_MULTI_TAX_PACKS[country])
    rows = [
        {"tax_category": "standard", "subtotal": std_subtotal},
        {"tax_category": "liquor", "subtotal": liq_subtotal},
        {"tax_category": "exempt", "subtotal": 3000},
    ]
    std, liq, _ = compute_category_breakdown(rows, cfg)
    assert std == expected_std
    assert liq == expected_liq
    assert resolve_tax_profile(cfg).line_for_category("exempt") is None


def test_wave1_included_gst_uses_extractive_math():
    cfg = tax_config_from_wave1_pack(WAVE1_TAX_PACKS["AU"])
    line = resolve_tax_profile(cfg).primary_line()
    assert line is not None
    assert line.included_in_price is True
    assert tax_amount_float(11000, line) == pytest.approx(1000.0)


class _PackConn:
    def __init__(self, *, tax_lines=None, update_affected=1):
        self.tax_lines = tax_lines
        self.queries = []
        self.update_affected = update_affected

    async def fetchrow(self, query, *args):
        self.queries.append((query, args))
        if "FROM tenant_tax_config" in query:
            if self.tax_lines is None and not any(
                "INSERT INTO tenant_tax_config" in q for q, _ in self.queries
            ):
                return None
            return {"tax_lines": self.tax_lines}
        raise AssertionError(f"Unexpected query: {query}")

    async def execute(self, query, *args):
        self.queries.append((query, args))
        if "INSERT INTO tenant_tax_config" in query:
            self.tax_lines = None
            return "INSERT 0 1"
        if "UPDATE tenant_tax_config" in query:
            if self.tax_lines is None:
                self.tax_lines = args[1]
            return f"UPDATE {self.update_affected}"
        raise AssertionError(f"Unexpected query: {query}")


@pytest.mark.asyncio
async def test_ensure_wave1_tax_pack_writes_when_missing():
    tenant_id = uuid4()
    conn = _PackConn(tax_lines=None)
    applied = await ensure_wave1_tax_pack(conn, tenant_id, "PA")
    assert applied is True
    assert any("UPDATE tenant_tax_config" in q for q, _ in conn.queries)


@pytest.mark.asyncio
async def test_ensure_country_tax_pack_writes_wave2_mx():
    tenant_id = uuid4()
    conn = _PackConn(tax_lines=None)
    applied = await ensure_country_tax_pack(conn, tenant_id, "MX")
    assert applied is True
    assert any("UPDATE tenant_tax_config" in q for q, _ in conn.queries)
    update = next(args for q, args in conn.queries if "UPDATE tenant_tax_config" in q)
    assert '"IVA 16%"' in update[1] or "0.16" in update[1]


@pytest.mark.asyncio
async def test_ensure_wave1_tax_pack_skips_colombia():
    tenant_id = uuid4()
    conn = _PackConn(tax_lines=None)
    applied = await ensure_wave1_tax_pack(conn, tenant_id, "CO")
    assert applied is False
    assert conn.queries == []


@pytest.mark.asyncio
async def test_ensure_wave1_tax_pack_does_not_overwrite_existing_lines():
    tenant_id = uuid4()
    conn = _PackConn(tax_lines=[{"key": "custom"}])
    applied = await ensure_wave1_tax_pack(conn, tenant_id, "CL")
    assert applied is False
    assert not any("UPDATE tenant_tax_config" in q for q, _ in conn.queries)


@pytest.mark.asyncio
async def test_ensure_country_tax_pack_does_not_overwrite_wave2():
    tenant_id = uuid4()
    conn = _PackConn(tax_lines=[{"key": "custom"}])
    applied = await ensure_country_tax_pack(conn, tenant_id, "MX")
    assert applied is False
    assert not any("UPDATE tenant_tax_config" in q for q, _ in conn.queries)




@pytest.mark.asyncio
async def test_ensure_country_tax_pack_writes_wave2_de():
    tenant_id = uuid4()
    conn = _PackConn(tax_lines=None)
    applied = await ensure_country_tax_pack(conn, tenant_id, "DE")
    assert applied is True
    assert any("UPDATE tenant_tax_config" in q for q, _ in conn.queries)
    update = next(args for q, args in conn.queries if "UPDATE tenant_tax_config" in q)
    assert "mwst_reduced" in update[1] and "mwst_standard" in update[1]


@pytest.mark.asyncio
async def test_ensure_country_tax_pack_does_not_overwrite_wave2_multi():
    tenant_id = uuid4()
    conn = _PackConn(tax_lines=[{"key": "custom"}])
    applied = await ensure_country_tax_pack(conn, tenant_id, "NL")
    assert applied is False
    assert not any("UPDATE tenant_tax_config" in q for q, _ in conn.queries)

def test_co_adapter_regression_unchanged():
    cfg = {
        "inc_applicable": True,
        "inc_rate": 0.08,
        "inc_included_in_price": True,
        "iva_applicable": False,
        "liquor_tax_applicable": False,
        "tax_lines": None,
        "category_map": None,
    }
    std, liq, _ = compute_category_breakdown(
        [{"tax_category": "standard", "subtotal": 10800}],
        cfg,
    )
    assert std == 800.0
    assert liq == 0.0
    assert resolve_tax_profile(cfg).primary_line().gl_role == "inc"

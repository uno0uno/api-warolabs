"""Tax lines matrix API helpers — uno0uno/warocol.com#1873."""
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.tax_config import TaxConfigUpdate
from app.services.tenant_config_service import (
    decode_tax_config_jsonb,
    validate_co_rate_fields,
    validate_tax_matrix_payload,
)


def _multi_lines():
    return [
        {
            "key": "mwst",
            "label": "MwSt 19%",
            "rate": 0.19,
            "included_in_price": True,
            "gl_role": "iva",
        },
        {
            "key": "mwst_reduced",
            "label": "MwSt 7%",
            "rate": 0.07,
            "included_in_price": True,
            "gl_role": "iva",
        },
    ]


def _category_map():
    return {"standard": "mwst", "liquor": "mwst_reduced", "exempt": None}


def test_decode_tax_config_jsonb_parses_asyncpg_strings():
    raw = {
        "tax_lines": '[{"key":"iva","rate":0.16}]',
        "category_map": '{"standard":"iva","exempt":null}',
        "iva_rate": Decimal("0.19"),
    }
    decoded = decode_tax_config_jsonb(raw)
    assert isinstance(decoded["tax_lines"], list)
    assert decoded["tax_lines"][0]["key"] == "iva"
    assert isinstance(decoded["category_map"], dict)
    assert decoded["category_map"]["standard"] == "iva"
    assert decoded["category_map"]["exempt"] is None


def test_decode_tax_config_jsonb_leaves_objects_alone():
    lines = _multi_lines()
    cmap = _category_map()
    decoded = decode_tax_config_jsonb({"tax_lines": lines, "category_map": cmap})
    assert decoded["tax_lines"] is lines
    assert decoded["category_map"] is cmap


def test_multi_line_matrix_round_trip_shape():
    """Simulates GET → PUT → GET payload survival for multi-line DE/NL-style matrix."""
    stored = {
        "tax_lines": _multi_lines(),
        "category_map": _category_map(),
        "commercial_tax_applicable": True,
    }
    # Client may receive jsonb as strings (asyncpg quirk).
    as_strings = {
        "tax_lines": __import__("json").dumps(stored["tax_lines"]),
        "category_map": __import__("json").dumps(stored["category_map"]),
        "commercial_tax_applicable": True,
    }
    got = decode_tax_config_jsonb(as_strings)
    validate_tax_matrix_payload(got["tax_lines"], got["category_map"])
    # PUT body mirrors GET objects; re-encode then decode again.
    put_encoded = {
        "tax_lines": __import__("json").dumps(got["tax_lines"]),
        "category_map": __import__("json").dumps(got["category_map"]),
    }
    again = decode_tax_config_jsonb(put_encoded)
    assert len(again["tax_lines"]) == 2
    assert {line["key"] for line in again["tax_lines"]} == {"mwst", "mwst_reduced"}
    assert again["category_map"] == _category_map()


def test_validate_rejects_negative_line_rate():
    with pytest.raises(HTTPException) as exc:
        validate_tax_matrix_payload(
            [{"key": "iva", "rate": -0.01}],
            {"standard": "iva", "exempt": None},
        )
    assert exc.value.status_code == 400
    assert "rate must be >= 0" in str(exc.value.detail)


def test_validate_rejects_unknown_category_map_key():
    with pytest.raises(HTTPException) as exc:
        validate_tax_matrix_payload(
            _multi_lines(),
            {"standard": "missing", "liquor": "mwst", "exempt": None},
        )
    assert exc.value.status_code == 400
    assert "unknown tax line key" in str(exc.value.detail)


def test_validate_allows_null_exempt_and_zero_rate():
    validate_tax_matrix_payload(
        [{"key": "vat", "rate": 0, "label": "VAT 0%"}],
        {"standard": "vat", "liquor": "vat", "exempt": None},
    )


def test_co_rate_fields_on_update_model():
    body = TaxConfigUpdate(
        inc_applicable=True,
        inc_included_in_price=True,
        iva_applicable=False,
        iva_included_in_price=False,
        liquor_tax_applicable=True,
        inc_rate=Decimal("0.08"),
        iva_rate=Decimal("0.19"),
        liquor_tax_rate=Decimal("0.05"),
    )
    validate_co_rate_fields(body)
    assert body.inc_rate == Decimal("0.08")
    assert body.liquor_tax_rate == Decimal("0.05")


def test_co_rate_rejects_negative():
    body = TaxConfigUpdate(
        inc_applicable=False,
        inc_included_in_price=False,
        iva_applicable=True,
        iva_included_in_price=False,
        liquor_tax_applicable=False,
        iva_rate=Decimal("-0.1"),
    )
    with pytest.raises(HTTPException) as exc:
        validate_co_rate_fields(body)
    assert exc.value.status_code == 400


def test_tax_config_update_rates_optional():
    body = TaxConfigUpdate(
        inc_applicable=False,
        inc_included_in_price=False,
        iva_applicable=False,
        iva_included_in_price=False,
        liquor_tax_applicable=False,
        tax_lines=_multi_lines(),
        category_map=_category_map(),
        commercial_tax_applicable=True,
    )
    assert body.iva_rate is None
    assert body.inc_rate is None
    assert body.liquor_tax_rate is None

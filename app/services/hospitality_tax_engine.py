"""Profile-driven hospitality tax lines for POS, orders, cierre, and tips.

Resolves tenant_tax_config into tax_lines[] + category→line map. Colombia's
INC/IVA/liquor columns adapt into the same shape so numeric results stay stable.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
import json


@dataclass(frozen=True)
class TaxLine:
    key: str
    label: str
    rate: float
    included_in_price: bool
    gl_role: str  # resolve_tax_account kind: inc | iva | liquor
    gl_account_id: Any = None
    gl_account_code: Optional[str] = None


@dataclass(frozen=True)
class TaxProfile:
    lines: Dict[str, TaxLine]
    category_map: Dict[str, Optional[str]]

    def line_for_category(self, category: str) -> Optional[TaxLine]:
        key = self.category_map.get(category or "standard")
        if not key:
            return None
        return self.lines.get(key)

    def primary_line(self) -> Optional[TaxLine]:
        return self.line_for_category("standard")


def _as_mapping_list(raw: Any) -> List[Mapping[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def _as_category_map(raw: Any) -> Dict[str, Optional[str]]:
    if raw is None:
        return {}
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, Mapping):
        return {}
    out: Dict[str, Optional[str]] = {}
    for key, value in raw.items():
        out[str(key)] = None if value in (None, "", "null") else str(value)
    return out


def _line_from_mapping(item: Mapping[str, Any]) -> Optional[TaxLine]:
    key = str(item.get("key") or "").strip()
    if not key:
        return None
    rate = float(item.get("rate") or 0)
    label = str(item.get("label") or key)
    included = bool(item.get("included_in_price", False))
    gl_role = str(item.get("gl_role") or "iva")
    return TaxLine(
        key=key,
        label=label,
        rate=rate,
        included_in_price=included,
        gl_role=gl_role,
        gl_account_id=item.get("gl_account_id"),
        gl_account_code=item.get("gl_account_code"),
    )


def _profile_from_co_columns(tax_config: Mapping[str, Any]) -> TaxProfile:
    lines: Dict[str, TaxLine] = {}
    category_map: Dict[str, Optional[str]] = {
        "standard": None,
        "liquor": None,
        "exempt": None,
    }

    if tax_config.get("inc_applicable"):
        rate = float(tax_config.get("inc_rate") or 0)
        lines["inc"] = TaxLine(
            key="inc",
            label=f"INC {round(rate * 100)}%",
            rate=rate,
            included_in_price=bool(tax_config.get("inc_included_in_price", True)),
            gl_role="inc",
            gl_account_id=tax_config.get("inc_gl_account_id"),
            gl_account_code=tax_config.get("inc_gl_account_code"),
        )
        category_map["standard"] = "inc"
    elif tax_config.get("iva_applicable"):
        rate = float(tax_config.get("iva_rate") or 0)
        lines["iva"] = TaxLine(
            key="iva",
            label=f"IVA {round(rate * 100)}%",
            rate=rate,
            included_in_price=bool(tax_config.get("iva_included_in_price", False)),
            gl_role="iva",
            gl_account_id=tax_config.get("iva_gl_account_id"),
            gl_account_code=tax_config.get("iva_gl_account_code"),
        )
        category_map["standard"] = "iva"

    if tax_config.get("liquor_tax_applicable"):
        rate = float(tax_config.get("liquor_tax_rate") or 0.05)
        lines["liquor"] = TaxLine(
            key="liquor",
            label="IVA licores 5%" if abs(rate - 0.05) < 1e-9 else f"IVA licores {round(rate * 100)}%",
            rate=rate,
            included_in_price=False,  # CO liquor tax is always additive
            gl_role="liquor",
            gl_account_id=tax_config.get("liquor_tax_gl_account_id"),
            gl_account_code=tax_config.get("liquor_tax_gl_account_code"),
        )
        category_map["liquor"] = "liquor"

    return TaxProfile(lines=lines, category_map=category_map)


def resolve_tax_profile(tax_config: Mapping[str, Any]) -> TaxProfile:
    """Build a TaxProfile from explicit tax_lines or CO column adapter."""
    raw_lines = _as_mapping_list(tax_config.get("tax_lines"))
    if raw_lines:
        lines: Dict[str, TaxLine] = {}
        for item in raw_lines:
            line = _line_from_mapping(item)
            if line:
                lines[line.key] = line
        category_map = _as_category_map(tax_config.get("category_map"))
        if not category_map:
            # Default commercial map: standard → first line; liquor → same; exempt → none
            first_key = next(iter(lines), None)
            category_map = {
                "standard": first_key,
                "liquor": first_key,
                "exempt": None,
            }
        else:
            category_map.setdefault("exempt", None)
            category_map.setdefault("standard", next(iter(lines), None))
            category_map.setdefault("liquor", category_map.get("standard"))
        return TaxProfile(lines=lines, category_map=category_map)

    return _profile_from_co_columns(tax_config)


def tax_amount_float(base: float, line: TaxLine) -> float:
    """POS/orders/tip style: whole-unit round after formula."""
    amount = float(base or 0)
    if amount <= 0 or line.rate <= 0:
        return 0.0
    if line.included_in_price:
        return float(round(amount * line.rate / (1 + line.rate)))
    return float(round(amount * line.rate))


def tax_amount_decimal(base: Decimal, line: TaxLine) -> Tuple[Decimal, bool]:
    """GL style: Decimal extractive/additive without intermediate float round.

    Returns (tax_amount, is_additive).
    """
    amount = Decimal(str(base or 0))
    rate = Decimal(str(line.rate or 0))
    if amount <= 0 or rate <= 0:
        return Decimal("0"), False
    if line.included_in_price:
        return amount - (amount / (1 + rate)), False
    return amount * rate, True


def compute_category_breakdown(
    items_rows: Sequence[Any],
    tax_config: Mapping[str, Any],
) -> Tuple[float, float, str]:
    """Compat wrapper: (standard_tax, liquor_tax, standard_tax_label)."""
    profile = resolve_tax_profile(tax_config)

    def _subtotal(category: str) -> float:
        total = 0.0
        for row in items_rows:
            cat = row["tax_category"] if hasattr(row, "__getitem__") else "standard"
            if cat == category:
                total += float(row["subtotal"])
        return total

    std_base = _subtotal("standard")
    liq_base = _subtotal("liquor")

    std_line = profile.line_for_category("standard")
    liq_line = profile.line_for_category("liquor")

    standard_tax = tax_amount_float(std_base, std_line) if std_line and std_base > 0 else 0.0
    liquor_tax = tax_amount_float(liq_base, liq_line) if liq_line and liq_base > 0 else 0.0
    label = std_line.label if std_line else "Impuesto"
    return float(standard_tax), float(liquor_tax), label


def compute_gl_category_taxes(
    standard_subtotal: Decimal,
    liquor_subtotal: Decimal,
    tax_config: Mapping[str, Any],
) -> Dict[str, Any]:
    """GL amounts for standard + liquor categories (cierre order post)."""
    profile = resolve_tax_profile(tax_config)
    std_line = profile.line_for_category("standard")
    liq_line = profile.line_for_category("liquor")

    standard_tax = Decimal("0")
    liquor_tax = Decimal("0")
    standard_is_additive = False
    standard_gl_role: Optional[str] = None
    liquor_gl_role: Optional[str] = None

    if std_line and standard_subtotal > 0:
        standard_tax, standard_is_additive = tax_amount_decimal(standard_subtotal, std_line)
        standard_gl_role = std_line.gl_role

    if liq_line and liquor_subtotal > 0:
        liquor_tax, _ = tax_amount_decimal(liquor_subtotal, liq_line)
        liquor_gl_role = liq_line.gl_role

    return {
        "standard_tax": standard_tax,
        "liquor_tax": liquor_tax,
        "standard_is_additive": standard_is_additive,
        "standard_gl_role": standard_gl_role,
        "liquor_gl_role": liquor_gl_role,
        "primary_line": profile.primary_line(),
        "profile": profile,
    }


def tip_tax_is_additive(tax_config: Mapping[str, Any]) -> bool:
    line = resolve_tax_profile(tax_config).primary_line()
    if not line:
        return False
    return not line.included_in_price


def primary_gl_role(tax_config: Mapping[str, Any]) -> Optional[str]:
    line = resolve_tax_profile(tax_config).primary_line()
    return line.gl_role if line else None

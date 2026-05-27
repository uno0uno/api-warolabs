"""
Tip tax helpers — warocol.com#740

Compute consumption tax on voluntary tips when the tenant/cashier opts in
(tip gravada). Mirrors POS receipt tax breakdown logic in pos_cart_service.
"""
from typing import Any, Dict, Tuple


VALID_TIP_SOURCES = ("preset", "custom", "none")


def normalize_tip_payload(
    tip_amount: float,
    tip_source: str,
    tip_taxable: bool = False,
) -> Tuple[float, str, bool]:
    """Normalize tip fields to the canonical order-header shape."""
    amount = float(tip_amount or 0)
    source = tip_source or "none"

    if amount < 0:
        raise ValueError("tip_amount must be non-negative")
    if source not in VALID_TIP_SOURCES:
        raise ValueError(f"invalid tip_source: {source!r}")
    if amount == 0:
        return 0.0, "none", False
    if source == "none":
        raise ValueError("tip_source cannot be 'none' when tip_amount > 0")
    return amount, source, bool(tip_taxable)


def compute_tip_tax_amount(
    tip_amount: float,
    tip_taxable: bool,
    tax_config: Dict[str, Any],
) -> float:
    """Return tax on tip in COP (whole pesos). Zero when not taxable or no tip."""
    if not tip_taxable or float(tip_amount or 0) <= 0:
        return 0.0
    amount = float(tip_amount)
    if tax_config.get("inc_applicable"):
        rate = float(tax_config["inc_rate"])
        if tax_config.get("inc_included_in_price"):
            return float(round(amount * rate / (1 + rate)))
        return float(round(amount * rate))
    if tax_config.get("iva_applicable"):
        rate = float(tax_config["iva_rate"])
        if tax_config.get("iva_included_in_price"):
            return float(round(amount * rate / (1 + rate)))
        return float(round(amount * rate))
    return 0.0


def tip_settlement_total(tip_amount: float, tip_tax_amount: float) -> float:
    """Tip + tax on tip for settlement / charged_amount."""
    return float(tip_amount or 0) + float(tip_tax_amount or 0)


def split_settlement_amount_due(
    order_total: float,
    tip_amount: float,
    tip_tax_amount: float = 0,
) -> float:
    """warocol.com#737 + #740 — order total + tip + tip tax."""
    return float(order_total) + tip_settlement_total(tip_amount, tip_tax_amount)

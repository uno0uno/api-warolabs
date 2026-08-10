"""Paddle regional pricing policy (epic #805 / batch #806).

List prices and default Paddle charge for new checkouts are monthly
(USD 9 / USD 30 / EUR 30). Annual 10× from #793 is legacy/display only.
Do not use subscription_plans.price_* for Paddle charge amounts.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Optional

from app.core.tenant_prefs import COUNTRY_CURRENCY_PAIRS

ProviderEnvironment = Literal["prod", "test"]
PriceSegment = Literal["usd_9", "usd_30", "eur_30"]

# Eurozone countries in COUNTRY_CURRENCY_PAIRS that charge in EUR.
EUROZONE_COUNTRIES = frozenset(
    code for code, currencies in COUNTRY_CURRENCY_PAIRS.items() if "EUR" in currencies
)

# Countries that charge in USD (dollarized).
USD_CHARGE_COUNTRIES = frozenset(
    code for code, currencies in COUNTRY_CURRENCY_PAIRS.items() if currencies == ("USD",)
) | frozenset({"PA"})  # PA supports USD/PAB; treat as dollarized list

# Strong-currency / intl allowlist → USD 30 (not USD 9).
INTL_USD_30_ALLOWLIST = frozenset({"GB", "CA", "AU", "NZ", "SG", "AE"})

# Tenants that always use Paddle sandbox (WARO Colombia internal / QA).
# Extend via settings later in #795 if needed.
PADDLE_SANDBOX_TENANT_SLUGS = frozenset(
    {
        "waro-colombia",
        "warocolombia",
    }
)

ANNUAL_MULTIPLIER = 10


@dataclass(frozen=True)
class PriceOffer:
    segment: PriceSegment
    currency: Literal["USD", "EUR"]
    monthly_amount_minor: int  # cents / euro cents
    annual_amount_minor: int
    paddle_price_id_test: str
    paddle_price_id_live: str

    def paddle_price_id(self, environment: ProviderEnvironment) -> str:
        return self.paddle_price_id_test if environment == "test" else self.paddle_price_id_live


# Placeholders until env PADDLE_PRICE_*_MONTHLY_* is set (prefer env via paddle_service).
SEGMENT_OFFERS: dict[PriceSegment, PriceOffer] = {
    "usd_9": PriceOffer(
        segment="usd_9",
        currency="USD",
        monthly_amount_minor=900,
        annual_amount_minor=900 * ANNUAL_MULTIPLIER,
        paddle_price_id_test="TODO_PADDLE_PRICE_USD_9_MONTHLY_TEST",
        paddle_price_id_live="TODO_PADDLE_PRICE_USD_9_MONTHLY_LIVE",
    ),
    "usd_30": PriceOffer(
        segment="usd_30",
        currency="USD",
        monthly_amount_minor=3000,
        annual_amount_minor=3000 * ANNUAL_MULTIPLIER,
        paddle_price_id_test="TODO_PADDLE_PRICE_USD_30_MONTHLY_TEST",
        paddle_price_id_live="TODO_PADDLE_PRICE_USD_30_MONTHLY_LIVE",
    ),
    "eur_30": PriceOffer(
        segment="eur_30",
        currency="EUR",
        monthly_amount_minor=3000,
        annual_amount_minor=3000 * ANNUAL_MULTIPLIER,
        paddle_price_id_test="TODO_PADDLE_PRICE_EUR_30_MONTHLY_TEST",
        paddle_price_id_live="TODO_PADDLE_PRICE_EUR_30_MONTHLY_LIVE",
    ),
}


def normalize_country_code(country_code: Optional[str]) -> str:
    """Blank/missing country defaults to CO (usd_9) — documented in paddle-pricing-policy.md."""
    if not country_code or not str(country_code).strip():
        return "CO"
    return str(country_code).strip().upper()


def resolve_price_segment(country_code: Optional[str]) -> PriceSegment:
    """Map tenant country to list segment (epic #793 + delta #794)."""
    code = normalize_country_code(country_code)
    if code in EUROZONE_COUNTRIES:
        return "eur_30"
    if code in USD_CHARGE_COUNTRIES or code in INTL_USD_30_ALLOWLIST:
        return "usd_30"
    return "usd_9"


def resolve_price_offer(country_code: Optional[str]) -> PriceOffer:
    return SEGMENT_OFFERS[resolve_price_segment(country_code)]


def resolve_provider_environment(
    *,
    tenant_slug: Optional[str] = None,
    billing_test: bool = False,
) -> ProviderEnvironment:
    """Paddle sandbox vs live.

    test: explicit billing_test OR WARO Colombia sandbox tenant slug.
    prod: everyone else (including Bubablue and other CO customers).
    """
    if billing_test:
        return "test"
    slug = (tenant_slug or "").strip().lower()
    if slug in PADDLE_SANDBOX_TENANT_SLUGS:
        return "test"
    return "prod"


def should_skip_mid_period_rebill(*, current_period_end_in_future: bool) -> bool:
    """True when period end is still ahead — skip mid-cycle rebill/reprice.

    Prefer `is_grandfathered_annual(...)` which also checks status + cycle.
    """
    return bool(current_period_end_in_future)


def is_grandfathered_annual(
    *,
    status: Optional[str],
    billing_cycle: Optional[str],
    current_period_end: Optional[datetime],
    now: Optional[datetime] = None,
) -> bool:
    """Active annual with future current_period_end — do not rebill mid-period (#797)."""
    if str(status or "").lower() != "active":
        return False
    if str(billing_cycle or "").lower() != "annual":
        return False
    if current_period_end is None:
        return False
    end = current_period_end
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    anchor = now or datetime.now(timezone.utc)
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    return should_skip_mid_period_rebill(current_period_end_in_future=end > anchor)


# Back-compat alias for early docs/tests.
grandfather_active_annual = should_skip_mid_period_rebill

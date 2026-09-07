"""Regional SaaS pricing policy (epic #805 / #941).

List prices and default MoR charge for new checkouts are monthly
(USD 9 / USD 30 / EUR 30). Annual 10× from #793 is legacy/display only.
Do not use subscription_plans.price_* for MoR charge amounts.
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

# Fallback QA allowlist when BILLING_SANDBOX_TENANT_SLUGS env is unset (#813 / #944).
DEFAULT_BILLING_SANDBOX_TENANT_SLUGS = frozenset(
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
    lemon_squeezy_variant_id_test: str
    lemon_squeezy_variant_id_live: str

    def lemon_squeezy_variant_id(self, environment: ProviderEnvironment) -> str:
        if environment == "test":
            return self.lemon_squeezy_variant_id_test
        return self.lemon_squeezy_variant_id_live


# Placeholders until env MoR variant IDs are set (prefer env via lemon_squeezy_service).
SEGMENT_OFFERS: dict[PriceSegment, PriceOffer] = {
    "usd_9": PriceOffer(
        segment="usd_9",
        currency="USD",
        monthly_amount_minor=900,
        annual_amount_minor=900 * ANNUAL_MULTIPLIER,
        lemon_squeezy_variant_id_test="TODO_LEMON_SQUEEZY_VARIANT_USD_9_MONTHLY_TEST",
        lemon_squeezy_variant_id_live="TODO_LEMON_SQUEEZY_VARIANT_USD_9_MONTHLY_LIVE",
    ),
    "usd_30": PriceOffer(
        segment="usd_30",
        currency="USD",
        monthly_amount_minor=3000,
        annual_amount_minor=3000 * ANNUAL_MULTIPLIER,
        lemon_squeezy_variant_id_test="TODO_LEMON_SQUEEZY_VARIANT_USD_30_MONTHLY_TEST",
        lemon_squeezy_variant_id_live="TODO_LEMON_SQUEEZY_VARIANT_USD_30_MONTHLY_LIVE",
    ),
    "eur_30": PriceOffer(
        segment="eur_30",
        currency="EUR",
        monthly_amount_minor=3000,
        annual_amount_minor=3000 * ANNUAL_MULTIPLIER,
        lemon_squeezy_variant_id_test="TODO_LEMON_SQUEEZY_VARIANT_EUR_30_MONTHLY_TEST",
        lemon_squeezy_variant_id_live="TODO_LEMON_SQUEEZY_VARIANT_EUR_30_MONTHLY_LIVE",
    ),
}


def normalize_country_code(country_code: Optional[str]) -> str:
    """Blank/missing country defaults to CO (usd_9) — docs/payments/saas-mor-pricing.md."""
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


def resolve_billing_sandbox_tenant_keys() -> frozenset[str]:
    """CSV from BILLING_SANDBOX_TENANT_SLUGS, or DEFAULT_BILLING_SANDBOX_TENANT_SLUGS."""
    from app.config import settings

    raw = (settings.billing_sandbox_tenant_slugs or "").strip()
    if not raw:
        return DEFAULT_BILLING_SANDBOX_TENANT_SLUGS
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


def resolve_provider_environment(
    *,
    tenant_slug: Optional[str] = None,
    tenant_id: Optional[str] = None,
    billing_test: bool = False,
) -> ProviderEnvironment:
    """Lemon Squeezy sandbox vs live (#813 / #942 / #944).

    test when:
    - billing_test=True, or
    - LEMON_SQUEEZY_ENVIRONMENT is sandbox|test (default sandbox for local/dev), or
    - production mode and tenant slug/id is in BILLING_SANDBOX_TENANT_SLUGS
      (empty env → DEFAULT_BILLING_SANDBOX_TENANT_SLUGS).

    prod: environment=production and tenant not on allowlist.
    """
    if billing_test:
        return "test"

    from app.config import settings

    ls_raw = settings.lemon_squeezy_environment
    mode = (
        ls_raw.strip().lower()
        if isinstance(ls_raw, str) and ls_raw.strip()
        else "sandbox"
    )
    if mode in ("sandbox", "test"):
        return "test"

    keys = resolve_billing_sandbox_tenant_keys()
    slug = (tenant_slug or "").strip().lower()
    if slug and slug in keys:
        return "test"
    tid = str(tenant_id).strip().lower() if tenant_id else ""
    if tid and tid in keys:
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

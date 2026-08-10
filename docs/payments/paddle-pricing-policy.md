# Paddle pricing policy (epic #805 / batch #806)

**Status:** Monthly is the **default charge cycle** for new checkouts.  
**Code:** `app/core/billing_pricing.py`, `app/services/paddle_service.py` (`configured_price_id`)  
**Related:** Epic [#805](https://github.com/uno0uno/api-warolabs/issues/805); annual 10× from [#793](https://github.com/uno0uno/api-warolabs/issues/793) is **legacy** only.

## List prices (monthly = charge basis)

| Segment | Monthly (charge) | Annual 10× (legacy display) | Who |
|---------|------------------|-----------------------------|-----|
| `usd_9` | **USD 9** | USD 90 | Non-dollarized (CO, MX, PE, …) except allowlist |
| `usd_30` | **USD 30** | USD 300 | US, PA; allowlist GB, CA, AU, NZ, SG, AE |
| `eur_30` | **EUR 30** | EUR 300 | Eurozone ES, DE, FR, NL |

Charge amounts use `monthly_amount_minor` (900 / 3000 / 3000). Do **not** charge from `subscription_plans.price_*` (legacy COP display).

## Env price IDs

Prefer monthly (required for new checkout once #807 unlocks the cycle):

- `PADDLE_PRICE_USD_9_MONTHLY_{TEST,LIVE}`
- `PADDLE_PRICE_USD_30_MONTHLY_{TEST,LIVE}`
- `PADDLE_PRICE_EUR_30_MONTHLY_{TEST,LIVE}`

Annual `PADDLE_PRICE_*_ANNUAL_*` is optional fallback only if monthly is unset. Segment placeholders are `TODO_PADDLE_PRICE_*_MONTHLY_*`.

**Deploy note:** Do not set monthly env in **prod** until subscribe defaults to monthly (#807); otherwise checkout may bill a monthly Paddle price while the API still labels the cycle annual.

## Country → segment

1. Country has EUR in `COUNTRY_CURRENCY_PAIRS` → `eur_30`
2. US / PA (dollarized) or intl allowlist → `usd_30`
3. Else → `usd_9`

## Sandbox vs live (`provider_environment`)

Driven by env (Wompi / Matías pattern) — not hardcoded QA slugs alone ([#813](https://github.com/uno0uno/api-warolabs/issues/813)):

| Env | Role |
|-----|------|
| `PADDLE_ENVIRONMENT` | `sandbox` (default) or `production`. Local/dev → leave default or set `sandbox` so **any** tenant uses Paddle Test. **Production deploys must set `production`.** |
| `PADDLE_SANDBOX_TENANT_SLUGS` | Optional CSV of tenant slugs and/or ids that stay on Test when `PADDLE_ENVIRONMENT=production`. Empty → fallback `warocolombia,waro-colombia`. |

| `provider_environment` | When |
|------------------------|------|
| `test` | `billing_test=True`, **or** `PADDLE_ENVIRONMENT` is `sandbox`/`test`, **or** (production mode and tenant on allowlist) |
| `prod` | `PADDLE_ENVIRONMENT=production` and tenant **not** on allowlist (e.g. Bubablue) |

## Grandfather (active annuals)

If `tenant_subscriptions.status = active` and `billing_cycle = annual` and `current_period_end` is still in the future:

- **Do not** rebill or force monthly mid-period.
- At period end / renew → monthly Paddle list and **`billing_cycle` flips to `monthly`** (+1 month) ([#809](https://github.com/uno0uno/api-warolabs/issues/809)).

Helpers:

- `is_grandfathered_annual(status=..., billing_cycle=..., current_period_end=...)`
- `should_skip_mid_period_rebill(current_period_end_in_future=...)` (period flag only)

## Missing country

Blank/missing `country_code` defaults to **CO** → `usd_9` (WARO Colombia product default).

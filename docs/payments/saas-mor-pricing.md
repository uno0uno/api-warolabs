# SaaS MoR pricing policy (Lemon Squeezy — epic #941 / #944)

Regional list prices for new SaaS checkouts are **monthly**:

| Segment | Currency | Monthly |
|---|---|---|
| `usd_9` | USD | 9.00 |
| `usd_30` | USD | 30.00 |
| `eur_30` | EUR | 30.00 |

Country → segment mapping lives in `app/core/billing_pricing.py`.

## Environment

| Variable | Role |
|---|---|
| `LEMON_SQUEEZY_ENVIRONMENT` | `sandbox` (default) or `production` |
| `BILLING_SANDBOX_TENANT_SLUGS` | Optional CSV of tenant slugs/ids that stay on Test when environment is production. Empty → fallback `warocolombia,waro-colombia`. |
| `LEMON_SQUEEZY_VARIANT_*_MONTHLY_*` | Variant IDs per segment (test/live) |

Amounts are unchanged from the former Paddle policy; only the Merchant of Record is Lemon Squeezy.

## Lemon Squeezy store: tax-inclusive pricing

Configure the LS store with **tax-inclusive pricing ON** so customers pay the advertised list price (e.g. USD 9.00 total) without VAT/sales tax added on top at checkout.

Webhook validation in `billing_service.lemon_squeezy_payment_matches_expected`:

| LS mode | `subtotal` (cents) | `total` charged (cents) | Accepted when list = 900 |
|---|---|---|---|
| Tax-exclusive | 900 | ≥ 900 (e.g. 1080 with tax) | Yes |
| Tax-inclusive | &lt; 900 (net) | == 900 | Yes |
| Tamper | &gt; 900 | any | No |
| Underpay | any | &lt; 900 | No |

Onboarding uses this check; renewals record the webhook `total` as the payment amount.

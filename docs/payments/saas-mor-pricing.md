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

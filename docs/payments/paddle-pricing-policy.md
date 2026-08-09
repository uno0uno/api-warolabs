# Paddle pricing policy (epic #793 / batch #794)

**Status:** Config + policy. Checkout/webhooks land in [#795](https://github.com/uno0uno/api-warolabs/issues/795).  
**Code:** `app/core/billing_pricing.py`

## List prices (monthly)

| Segment | Monthly | Annual (10×) | Who |
|---------|---------|--------------|-----|
| `usd_9` | USD 9 | **USD 90** | Non-dollarized (CO, MX, PE, …) except allowlist |
| `usd_30` | USD 30 | **USD 300** | US, PA; allowlist GB, CA, AU, NZ, SG, AE |
| `eur_30` | EUR 30 | **EUR 300** | Eurozone ES, DE, FR, NL |

Annual multiplier **10×** matches “~2 months free” psychology (subscribe API is annual-only today).

**Do not** charge from `subscription_plans.price_monthly` / `price_annual` (legacy COP display). Paddle uses segment → `paddle_price_id_*` (placeholders `TODO_PADDLE_PRICE_*` until catalog exists).

## Country → segment

1. Country has EUR in `COUNTRY_CURRENCY_PAIRS` → `eur_30`
2. US / PA (dollarized) or intl allowlist → `usd_30`
3. Else → `usd_9`

## Sandbox vs live (`provider_environment`)

| Value | When |
|-------|------|
| `test` | `billing_test=True` **or** tenant slug in `PADDLE_SANDBOX_TENANT_SLUGS` (e.g. `warocolombia`) |
| `prod` | All other tenants (including Colombian customers like Bubablue) |

Mirrors the intent of Wompi sandbox-for-WARO-CO / live-elsewhere without putting every `CO` tenant in sandbox.

## Grandfather (active annuals)

If `tenant_subscriptions.status = active` and `billing_cycle = annual` and `current_period_end` is still in the future:

- **Do not** rebill or force the new Paddle list mid-period.
- At period end / renew → Paddle + regional list ([#797](https://github.com/uno0uno/api-warolabs/issues/797)).

Helper: `grandfather_active_annual(current_period_end_in_future=...)`.

## Out of scope here

Paddle SDK, webhooks, front CTA, Wompi removal.

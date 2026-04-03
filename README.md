# WaRo Colombia — Public API

REST API for integrating WaRo into your restaurant's ordering flow, analytics pipelines, and loyalty programs.

All v1 endpoints authenticate via API key (`x-api-key` header). Keys are issued per tenant from the WaRo dashboard.

> **Docs** — Swagger UI at `/docs` · Postman collection in `postman/`

---

## Sales & Orders

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/v1/sales` | List sales with filters |
| `POST` | `/v1/sales/metrics` | Aggregated sales metrics |
| `POST` | `/v1/sales/detail` | Full detail of a single sale |

## Menu

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/v1/menu` | Full menu with categories |
| `POST` | `/v1/menu/products` | Product list with filters |
| `POST` | `/v1/menu/recipes` | Recipe list |
| `POST` | `/v1/menu/modifiers` | Modifier groups |
| `GET` | `/v1/product/{id}` | Product detail with modifier groups |

## Customers

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/v1/customers` | Customer list |
| `POST` | `/v1/customers/detail` | Customer profile |
| `POST` | `/v1/customers/orders` | Order history per customer |
| `POST` | `/v1/customers/metrics` | Customer analytics |

## Analytics

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/v1/analytics/rfm` | RFM segmentation |
| `POST` | `/v1/analytics/cohort` | Retention cohort matrix |
| `POST` | `/v1/analytics/churn-risk` | Churn risk scoring |
| `POST` | `/v1/analytics/menu-analysis` | Menu BCG analysis |
| `POST` | `/v1/analytics/food-cost` | Food cost analysis |
| `POST` | `/v1/analytics/alerts` | Operational alerts |
| `POST` | `/v1/analytics/data-quality` | Data quality score |
| `POST` | `/v1/analytics/waros` | WaRos loyalty analytics |

## Financial

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/v1/financial/products` | Product profitability |

## WaRos Loyalty

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/v1/waros/customer-summary` | Customer loyalty summary |
| `POST` | `/v1/waros/balances` | Points balances |
| `POST` | `/v1/waros/estimate` | Points estimate for an order |
| `POST` | `/v1/waros/customer-history` | Transaction history |

## Online Ordering

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/v1/restaurant` | Restaurant info |
| `POST` | `/v1/cart/batch` | Create / update cart |
| `GET` | `/v1/cart/session/{session_id}` | Get cart by session |
| `PUT` | `/v1/cart/{id}/delivery` | Set delivery details |
| `DELETE` | `/v1/cart/{id}/items/{item_id}` | Remove item |
| `DELETE` | `/v1/cart/{id}` | Clear cart |
| `POST` | `/v1/cart/{id}/verify` | Verify cart before checkout |
| `POST` | `/v1/cart/{id}/checkout` | Place order |
| `POST` | `/v1/otp/send` | Send OTP to customer |
| `POST` | `/v1/otp/verify` | Verify OTP — returns customer JWT |
| `POST` | `/v1/otp/resend` | Resend OTP |
| `POST` | `/v1/customer/validate` | Validate customer session |
| `POST` | `/v1/addresses` | Create delivery address |
| `GET` | `/v1/addresses/customer/{id}` | List customer addresses |
| `PUT` | `/v1/addresses/{id}` | Update address |
| `DELETE` | `/v1/addresses/{id}` | Delete address |
| `PATCH` | `/v1/addresses/{id}/set-default` | Set default address |

---

## CLI

The [waro-cli](https://github.com/uno0uno/waro-cli) provides scriptable access to all v1 endpoints — optimized for shell pipelines and AI agents.

```bash
waro sales list --date-from 2026-03-01 --output table
waro sales metrics --group-by date
waro schema sales list   # introspect endpoint schema
```

---

## License

Proprietary — [warocol.com](https://warocol.com)

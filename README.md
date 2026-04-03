# WaRo Colombia — Public API

Backend service powering [warocol.com](https://warocol.com) and the WaRo public integration API.
Built with FastAPI + asyncpg on Python 3.9.

> **API documentation** — Swagger UI available at `/docs` (v1 endpoints are the canonical integration surface; all others are deprecated).
> Postman collection available in the `postman/` directory.

---

## Public API v1

All v1 endpoints authenticate via API key (`x-api-key` header). Keys are issued per tenant from the WaRo dashboard.

### Sales & Orders
| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/v1/sales` | List sales with filters |
| `POST` | `/v1/sales/metrics` | Aggregated sales metrics |
| `POST` | `/v1/sales/detail` | Full detail of a single sale |

### Menu
| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/v1/menu` | Full menu with categories |
| `POST` | `/v1/menu/products` | Product list with filters |
| `POST` | `/v1/menu/recipes` | Recipe list |
| `POST` | `/v1/menu/modifiers` | Modifier groups |
| `GET` | `/v1/product/{id}` | Product detail with modifier groups |

### Customers
| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/v1/customers` | Customer list |
| `POST` | `/v1/customers/detail` | Customer profile |
| `POST` | `/v1/customers/orders` | Order history per customer |
| `POST` | `/v1/customers/metrics` | Customer analytics |

### Analytics
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

### Financial
| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/v1/financial/products` | Product profitability |

### WaRos Loyalty
| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/v1/waros/customer-summary` | Customer loyalty summary |
| `POST` | `/v1/waros/balances` | Points balances |
| `POST` | `/v1/waros/estimate` | Points estimate for an order |
| `POST` | `/v1/waros/customer-history` | Transaction history |

### Online Ordering (cart + checkout)
| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/v1/restaurant` | Restaurant info |
| `POST` | `/v1/cart/batch` | Create/update cart |
| `GET` | `/v1/cart/session/{session_id}` | Get cart by session |
| `PUT` | `/v1/cart/{id}/delivery` | Set delivery details |
| `DELETE` | `/v1/cart/{id}/items/{item_id}` | Remove item |
| `DELETE` | `/v1/cart/{id}` | Clear cart |
| `POST` | `/v1/cart/{id}/verify` | Verify cart before checkout |
| `POST` | `/v1/cart/{id}/checkout` | Place order |
| `POST` | `/v1/otp/send` | Send OTP to customer |
| `POST` | `/v1/otp/verify` | Verify OTP → returns customer JWT |
| `POST` | `/v1/otp/resend` | Resend OTP |
| `POST` | `/v1/customer/validate` | Validate customer session |
| `POST` | `/v1/addresses` | Create delivery address |
| `GET` | `/v1/addresses/customer/{id}` | List customer addresses |
| `PUT` | `/v1/addresses/{id}` | Update address |
| `DELETE` | `/v1/addresses/{id}` | Delete address |
| `PATCH` | `/v1/addresses/{id}/set-default` | Set default address |

---

## CLI Integration

The [waro-cli](https://github.com/uno0uno/waro-cli) provides scriptable access to all v1 endpoints — optimized for shell pipelines and AI agents.

```bash
# Install
curl -fsSL https://raw.githubusercontent.com/uno0uno/waro-cli/main/install.sh | sh

# Configure
export WARO_API_KEY=waro_sk_your_key_here

# Examples
waro sales list --date-from 2026-03-01 --output table
waro sales metrics --group-by date
waro analytics rfm
waro schema sales list   # Introspect endpoint schema (useful for AI agents)
```

---

## Quick Start

### Prerequisites
- Python 3.9+
- Docker and Docker Compose
- PostgreSQL database

### Environment setup

```bash
cp .env.example .env
# Edit .env with your database credentials
```

```env
NUXT_PRIVATE_DB_USER=your_db_user
NUXT_PRIVATE_DB_HOST=your_db_host
NUXT_PRIVATE_DB_PASSWORD=your_db_password
NUXT_PRIVATE_DB_PORT=5432
NUXT_PRIVATE_DB_NAME=your_db_name
NUXT_PRIVATE_JWT_SECRET=your_jwt_secret
```

### Local development

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 5001 --reload
```

### Docker (development)

```bash
docker-compose up --build
# Available at http://localhost:5001
```

### Docker (production — zero downtime)

```bash
# Build while old container keeps serving traffic, then swap
docker-compose -f docker-compose.yml -f docker-compose.prod.yml build
docker-compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

> Never use `up -d --build` in production — it stops the container during the build.

---

## API Documentation

| Interface | URL |
|-----------|-----|
| Swagger UI | `http://localhost:5001/docs` |
| ReDoc | `http://localhost:5001/redoc` |
| Postman | `postman/Waro_Public_API.postman_collection.json` |

Authenticate in Swagger using the lock icon on any `/v1` endpoint — enter your `waro_sk_...` API key.

---

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `NUXT_PRIVATE_DB_USER` | Database username | Yes |
| `NUXT_PRIVATE_DB_HOST` | Database host | Yes |
| `NUXT_PRIVATE_DB_PASSWORD` | Database password | Yes |
| `NUXT_PRIVATE_DB_PORT` | Database port | Yes |
| `NUXT_PRIVATE_DB_NAME` | Database name | Yes |
| `NUXT_PRIVATE_JWT_SECRET` | JWT secret | Yes |
| `DEBUG` | Enable debug mode | No |
| `CORS_ORIGINS` | Allowed origins (comma-separated) | No |

---

## License

Proprietary — [warocol.com](https://warocol.com)

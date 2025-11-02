# API WaroLabs

FastAPI service for warocol.com providing financial endpoints and authentication.

## Features

- **Financial Analysis**: TIR metrics, product analysis, and obstacles analysis
- **Authentication**: Session-based authentication with cookie support
- **Docker Support**: Containerized deployment with development/production configurations
- **Database Integration**: PostgreSQL connection with asyncpg
- **API Documentation**: Swagger UI with cookie authentication support

## Quick Start

### Prerequisites

- Python 3.9+
- Docker and Docker Compose
- PostgreSQL database

### Installation

1. **Clone the repository**
   ```bash
   git clone https://github.com/uno0uno/api-warolabs.git
   cd api-warolabs
   ```

2. **Environment Setup**
   
   Copy and configure your environment variables:
   ```bash
   cp .env.example .env
   ```
   
   Update `.env` with your database and JWT configuration:
   ```env
   NUXT_PRIVATE_DB_USER=your_db_user
   NUXT_PRIVATE_DB_HOST=your_db_host
   NUXT_PRIVATE_DB_PASSWORD=your_db_password
   NUXT_PRIVATE_DB_PORT=5432
   NUXT_PRIVATE_DB_NAME=your_db_name
   NUXT_PRIVATE_JWT_SECRET=your_jwt_secret
   ```

### Development

#### Option 1: Local Development
```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run development server
uvicorn app.main:app --host 0.0.0.0 --port 5001 --reload
```

#### Option 2: Docker Development
```bash
# Start with hot reload (development)
docker-compose up --build

# The service will be available at http://localhost:5001
```

### Production

```bash
# Production deployment
docker-compose -f docker-compose.yml -f docker-compose.prod.yml up --build -d
```

## API Endpoints

### Authentication
- `GET /auth/session` - Get current session data
- `POST /auth/signout` - Sign out user

### Financial Analysis
- `GET /finance/tir-metrics` - Get TIR financial metrics
- `GET /finance/products-analysis` - Get product sales and profitability analysis
- `GET /finance/obstacles-analysis` - Get business operational obstacles analysis

### Health Check
- `GET /` - Service information
- `GET /health` - Health status

## API Documentation

Once running, access the interactive API documentation:
- **Swagger UI**: http://localhost:5001/docs
- **ReDoc**: http://localhost:5001/redoc

## Configuration

### Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `NUXT_PRIVATE_DB_USER` | Database username | Yes |
| `NUXT_PRIVATE_DB_HOST` | Database host | Yes |
| `NUXT_PRIVATE_DB_PASSWORD` | Database password | Yes |
| `NUXT_PRIVATE_DB_PORT` | Database port | Yes |
| `NUXT_PRIVATE_DB_NAME` | Database name | Yes |
| `NUXT_PRIVATE_JWT_SECRET` | JWT secret for authentication | Yes |
| `DEBUG` | Enable debug mode | No |

### Docker Compose Files

- `docker-compose.yml` - Base configuration
- `docker-compose.override.yml` - Development overrides (auto-applied)
- `docker-compose.prod.yml` - Production configuration

## Database Schema

The service expects a PostgreSQL database with the following key tables:
- `sessions` - User session management
- `tenant_members` - Multi-tenant user relationships
- `orders`, `payments` - Financial transaction data
- `products`, `product_variants` - Product catalog
- `inventory_transactions` - Stock management

## CORS Configuration

Configured for warocol.com compatibility:
- Development: `http://localhost:8080`
- Production: `https://warocol.com`

## License

This project is proprietary software for warocol.com.

## Support

For questions or issues, please contact the development team.
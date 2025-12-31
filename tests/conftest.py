"""
Pytest configuration and fixtures for API testing.

This module provides:
- Async test client for FastAPI
- Mock fixtures for session and tenant contexts
- Database fixtures for integration tests
"""
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from typing import Dict, Any
from datetime import datetime, timedelta

from app.main import app
from app.core.middleware import SessionContext, TenantContext


# Test tenant data (Waro Colombia)
TEST_TENANT_DATA = {
    "tenant_id": "93b3e582-34fa-44a6-8d0f-bf82a3608727",
    "tenant_name": "Waro Colombia",
    "tenant_slug": "warocolombia",
    "tenant_email": "anderson.arevalo@warocol.com",
    "site": "warocol.com",
    "brand_name": "Waro Colombia",
    "is_active": True
}

# Test user data (for mocked sessions)
TEST_USER_DATA = {
    "user_id": "test-user-id-12345",
    "tenant_id": "93b3e582-34fa-44a6-8d0f-bf82a3608727",
    "email": "test@warocol.com",
    "name": "Test User",
    "expires_at": datetime.now() + timedelta(days=7),
    "is_active": True
}


@pytest.fixture(scope="session")
def anyio_backend():
    """Required for pytest-asyncio"""
    return "asyncio"


@pytest_asyncio.fixture
async def client() -> AsyncClient:
    """
    Create an async test client without authentication.
    Use for testing public endpoints.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={
            "Origin": "https://warocol.com",
            "Referer": "https://warocol.com/"
        }
    ) as ac:
        yield ac


@pytest_asyncio.fixture
async def auth_client() -> AsyncClient:
    """
    Create an async test client with mocked authentication.
    Use for testing protected endpoints.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={
            "Origin": "https://warocol.com",
            "Referer": "https://warocol.com/",
            "Content-Type": "application/json"
        }
    ) as ac:
        yield ac


def create_mock_session_context(
    user_id: str = TEST_USER_DATA["user_id"],
    tenant_id: str = TEST_USER_DATA["tenant_id"],
    email: str = TEST_USER_DATA["email"],
    name: str = TEST_USER_DATA["name"],
    is_valid: bool = True
) -> SessionContext:
    """Create a mock SessionContext for testing."""
    if not is_valid:
        return SessionContext()

    return SessionContext({
        "user_id": user_id,
        "tenant_id": tenant_id,
        "email": email,
        "name": name,
        "expires_at": datetime.now() + timedelta(days=7),
        "is_active": True
    })


def create_mock_tenant_context(
    tenant_id: str = TEST_TENANT_DATA["tenant_id"],
    tenant_name: str = TEST_TENANT_DATA["tenant_name"],
    is_valid: bool = True
) -> TenantContext:
    """Create a mock TenantContext for testing."""
    if not is_valid:
        return TenantContext()

    return TenantContext({
        **TEST_TENANT_DATA,
        "tenant_id": tenant_id,
        "tenant_name": tenant_name
    })


@pytest.fixture
def mock_session() -> Dict[str, Any]:
    """Return mock session data for testing"""
    return TEST_USER_DATA.copy()


@pytest.fixture
def mock_tenant() -> Dict[str, Any]:
    """Return mock tenant data for testing"""
    return TEST_TENANT_DATA.copy()


def pytest_configure(config):
    """Configure pytest markers"""
    config.addinivalue_line(
        "markers", "integration: mark test as integration test (uses real database)"
    )
    config.addinivalue_line(
        "markers", "unit: mark test as unit test (mocked dependencies)"
    )

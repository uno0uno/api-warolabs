"""Magic link finds profile by case-insensitive email after normalization."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.services.magic_link_service import send_magic_link


@pytest.mark.asyncio
async def test_send_magic_link_normalizes_mixed_case_email():
    tenant_id = uuid4()
    user_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "user_id": user_id,
                "email": "sofiarengifo1302@gmail.com",
                "name": "sofia",
                "role": "employee",
                "tenant_id": tenant_id,
            },
            {
                "tenant_name": "Tijuana",
                "tenant_email": "a@b.com",
                "brand_name": "Tijuana",
            },
        ]
    )
    conn.execute = AsyncMock()

    @asynccontextmanager
    async def db_ctx(**_kwargs):
        yield conn

    mock_tenant = SimpleNamespace(
        site="tijuana.test",
        tenant_name="Tijuana",
        tenant_id=tenant_id,
        tenant_email="a@b.com",
        brand_name="Tijuana",
    )

    mock_request = AsyncMock()
    mock_request.headers = {"origin": "http://localhost:8080"}

    with patch(
        "app.services.magic_link_service.require_valid_tenant",
        return_value=mock_tenant,
    ), patch(
        "app.services.magic_link_service.get_db_connection",
        side_effect=db_ctx,
    ), patch(
        "app.services.aws_ses_service.ses_service.send_email",
        new=AsyncMock(return_value=True),
    ), patch(
        "app.config.settings",
    ) as mock_settings:
        mock_settings.is_development = True
        await send_magic_link(mock_request, "Sofiarengifo1302@gmail.com")

    lookup_email = conn.fetchrow.await_args_list[0].args[1]
    assert lookup_email == "sofiarengifo1302@gmail.com"

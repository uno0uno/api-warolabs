"""Magic link resolves internal members on their restaurant tenant from warocol.com."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.services.magic_link_service import send_magic_link


def _branding_row(name="Natural Food"):
    return {
        "tenant_name": name,
        "tenant_email": "natural@example.com",
        "brand_name": name,
    }


@pytest.mark.asyncio
async def test_send_magic_link_uses_first_internal_membership_from_any_tenant():
    site_tenant_id = uuid4()
    natural_food_id = uuid4()
    user_id = uuid4()

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "user_id": user_id,
                "email": "nadia.sanchezm@hotmail.com",
                "name": "Nadia Sánchez",
                "role": "superuser",
                "tenant_id": natural_food_id,
            },
            _branding_row(),
        ]
    )
    conn.execute = AsyncMock()

    @asynccontextmanager
    async def db_ctx(**_kwargs):
        yield conn

    mock_tenant = SimpleNamespace(
        site="warocol.com",
        tenant_name="Waro Colombia",
        tenant_id=site_tenant_id,
        tenant_email="admin@warocol.com",
        brand_name="WARO",
    )

    mock_request = AsyncMock()
    mock_request.headers = {"origin": "https://warocol.com"}

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
        mock_settings.is_development = False
        await send_magic_link(mock_request, "nadia.sanchezm@hotmail.com")

    lookup_args = conn.fetchrow.await_args_list[0].args
    assert lookup_args[1] == "nadia.sanchezm@hotmail.com"
    assert site_tenant_id not in lookup_args

    token_insert = conn.execute.await_args_list[1]
    assert token_insert.args[5] == natural_food_id

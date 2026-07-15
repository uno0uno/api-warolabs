from unittest.mock import AsyncMock, patch

import pytest

from app.services.discord_service import DiscordWebhookService
from app.services.leads_service import notify_self_service_registration


@pytest.mark.asyncio
async def test_self_service_notification_uses_discord_only_with_full_attribution():
    discord = AsyncMock()
    discord.notify_new_lead = AsyncMock(return_value=True)

    with patch("app.services.discord_service.discord_leads_service", discord), patch(
        "app.services.aws_ses_service.ses_service.send_email", new=AsyncMock()
    ) as ses:
        await notify_self_service_registration(
            email="verified@example.com",
            phone="3001234567",
            phone_country_code=57,
            tenant_name="Mi Restaurante",
            status="business_profile_pending",
            source="blog",
            content="inventory-cta",
            campaign="trial-launch",
            variant="price",
        )

    discord.notify_new_lead.assert_awaited_once_with(
        email="verified@example.com",
        phone="3001234567",
        phone_country_code=57,
        button_source="self_service_registration",
        tenant_name="Mi Restaurante",
        status="business_profile_pending",
        source="blog",
        content="inventory-cta",
        campaign="trial-launch",
        variant="price",
    )
    ses.assert_not_awaited()


@pytest.mark.asyncio
async def test_self_service_notification_failure_is_non_blocking():
    discord = AsyncMock()
    discord.notify_new_lead = AsyncMock(side_effect=RuntimeError("offline"))
    with patch("app.services.discord_service.discord_leads_service", discord):
        await notify_self_service_registration(
            email="verified@example.com",
            phone=None,
            phone_country_code=None,
            tenant_name=None,
            status="business_profile_pending",
            source=None,
            content=None,
            campaign=None,
            variant=None,
        )


@pytest.mark.asyncio
async def test_discord_formats_non_colombian_self_service_registration():
    service = DiscordWebhookService("https://discord.invalid")
    service.send_notification = AsyncMock(return_value=True)

    await service.notify_new_lead(
        email="owner@example.com",
        phone="5551234567",
        phone_country_code=52,
        button_source="self_service_registration",
        tenant_name="Taquería",
        status="business_profile_pending",
        source="home",
        campaign="trial",
        variant="a",
    )

    kwargs = service.send_notification.await_args.kwargs
    assert kwargs["title"] == "🚀 Nuevo registro autogestionado"
    assert "**Teléfono:** +52 5551234567" in kwargs["description"]
    assert "**Negocio:** Taquería" in kwargs["description"]
    assert "**Source:** home" in kwargs["description"]

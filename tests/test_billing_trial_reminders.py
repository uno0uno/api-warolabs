from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.services import billing_email_service


def _candidate():
    return {
        "subscription_id": uuid4(),
        "tenant_id": uuid4(),
        "tenant_name": "Restaurante",
        "tenant_email": "owner@example.com",
        "days_remaining": 7,
        "trial_ends_at": datetime(2026, 7, 22, tzinfo=timezone.utc),
    }


@pytest.mark.asyncio
async def test_successful_trial_warning_records_event_once_without_pii_arguments():
    conn = AsyncMock()
    trial = _candidate()

    with patch.object(
        billing_email_service,
        "get_trial_warning_candidates",
        new=AsyncMock(return_value=[trial]),
    ), patch.object(
        billing_email_service,
        "trial_warning_already_sent",
        new=AsyncMock(return_value=False),
    ), patch.object(
        billing_email_service,
        "send_trial_warning",
        new=AsyncMock(return_value=True),
    ), patch.object(
        billing_email_service,
        "record_trial_warning_sent",
        new=AsyncMock(),
    ) as record:
        result = await billing_email_service.process_trial_warnings(conn)

    assert result == {"sent": 1, "skipped": 0, "error": 0}
    assert record.await_args.kwargs == {
        "tenant_id": trial["tenant_id"],
        "subscription_id": trial["subscription_id"],
        "days_remaining": 7,
        "trial_ends_at": trial["trial_ends_at"],
    }
    assert "tenant_email" not in record.await_args.kwargs


@pytest.mark.asyncio
async def test_failed_trial_warning_does_not_record_sent_event():
    conn = AsyncMock()
    trial = _candidate()

    with patch.object(
        billing_email_service,
        "get_trial_warning_candidates",
        new=AsyncMock(return_value=[trial]),
    ), patch.object(
        billing_email_service,
        "trial_warning_already_sent",
        new=AsyncMock(return_value=False),
    ), patch.object(
        billing_email_service,
        "send_trial_warning",
        new=AsyncMock(return_value=False),
    ), patch.object(
        billing_email_service,
        "record_trial_warning_sent",
        new=AsyncMock(),
    ) as record:
        result = await billing_email_service.process_trial_warnings(conn)

    assert result == {"sent": 0, "skipped": 0, "error": 1}
    record.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_trial_warning_is_skipped_before_ses():
    conn = AsyncMock()
    trial = _candidate()

    with patch.object(
        billing_email_service,
        "get_trial_warning_candidates",
        new=AsyncMock(return_value=[trial]),
    ), patch.object(
        billing_email_service,
        "trial_warning_already_sent",
        new=AsyncMock(return_value=True),
    ), patch.object(
        billing_email_service,
        "send_trial_warning",
        new=AsyncMock(),
    ) as send:
        result = await billing_email_service.process_trial_warnings(conn)

    assert result == {"sent": 0, "skipped": 1, "error": 0}
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_trial_warning_uses_ses_but_returns_only_success_flag():
    trial = _candidate()

    with patch.object(
        billing_email_service._ses,
        "send_email",
        new=AsyncMock(return_value=True),
    ) as send:
        result = await billing_email_service.send_trial_warning(trial)

    assert result is True
    assert send.await_args.kwargs["to_emails"] == ["owner@example.com"]
    assert "owner@example.com" not in send.await_args.kwargs["text_body"]

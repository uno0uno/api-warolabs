from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.routers import billing
from app.services import billing_service, onboarding_service


@pytest.mark.asyncio
async def test_terms_retry_returns_existing_trial_without_new_acceptance():
    tenant_id = uuid4()
    subscription_id = uuid4()
    started = datetime(2026, 7, 15, tzinfo=timezone.utc)
    ended = started + timedelta(days=15)
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[
        {
            "tenant_id": tenant_id,
            "tenant_name": "Restaurante",
            "tenant_email": "owner@example.com",
            "lifecycle_status": "active",
            "onboarding_id": uuid4(),
            "state": "active",
            "owner_user_id": uuid4(),
            "owner_member_id": uuid4(),
            "country_code": "CO",
        },
        {
            "id": subscription_id,
            "status": "trialing",
            "trial_started_at": started,
            "trial_ends_at": ended,
        },
    ])

    with patch.object(
        onboarding_service.legal_service,
        "accept_current_terms",
        new=AsyncMock(),
    ) as accept:
        result = await onboarding_service.accept_onboarding_terms(
            conn,
            SimpleNamespace(tenant_id=tenant_id),
            client_ip="203.0.113.1",
            user_agent="pytest",
        )

    accept.assert_not_awaited()
    assert result["data"]["already_accepted"] is True
    assert result["data"]["trial"]["subscriptionId"] == str(subscription_id)
    assert conn.execute.await_count == 0


@pytest.mark.asyncio
async def test_expire_due_trials_is_single_sql_transition_with_durable_event():
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[{"subscription_id": uuid4()}])

    expired = await billing_service.expire_due_trials(conn)

    assert expired == 1
    sql = " ".join(conn.fetch.await_args.args[0].split())
    assert "status = 'trial_expired'" in sql
    assert "trial_ends_at <= now()" in sql
    assert "'trial_expired'" in sql
    assert "email" not in sql.lower()
    assert "phone" not in sql.lower()


@pytest.mark.asyncio
async def test_trial_warning_candidate_uses_ceiling_day_bucket():
    frozen = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[{
        "subscription_id": uuid4(),
        "tenant_id": uuid4(),
        "trial_ends_at": frozen + timedelta(days=6, hours=1),
        "tenant_name": "Restaurante",
        "tenant_email": "owner@example.com",
    }])

    with patch.object(billing_service, "datetime") as mocked_datetime:
        mocked_datetime.now.return_value = frozen
        candidates = await billing_service.get_trial_warning_candidates(conn)

    assert candidates[0]["days_remaining"] == 7


@pytest.mark.asyncio
async def test_record_trial_warning_metadata_has_no_recipient_pii():
    conn = AsyncMock()
    trial_end = datetime(2026, 7, 22, tzinfo=timezone.utc)

    await billing_service.record_trial_warning_sent(
        conn,
        tenant_id=uuid4(),
        subscription_id=uuid4(),
        days_remaining=7,
        trial_ends_at=trial_end,
    )

    metadata = conn.execute.await_args.args[4]
    assert "days_remaining" in metadata
    assert "trial_ends_at" in metadata
    assert "email" not in metadata
    assert "phone" not in metadata
    assert "owner@example.com" not in metadata


@pytest.mark.asyncio
async def test_trial_warning_claim_is_atomic_and_contains_no_recipient_pii():
    conn = AsyncMock()
    conn.fetchval.return_value = 1
    tenant_id = uuid4()
    subscription_id = uuid4()
    trial_end = datetime(2026, 7, 22, tzinfo=timezone.utc)

    claimed = await billing_service.claim_trial_warning_delivery(
        conn,
        tenant_id=tenant_id,
        subscription_id=subscription_id,
        days_remaining=7,
        trial_ends_at=trial_end,
    )

    assert claimed is True
    query, *arguments = conn.fetchval.await_args.args
    assert "ON CONFLICT (subscription_id, days_remaining)" in query
    assert "RETURNING 1" in query
    assert arguments == [subscription_id, tenant_id, 7, trial_end]
    assert "email" not in query.lower()
    assert "phone" not in query.lower()


@pytest.mark.asyncio
async def test_trial_checkout_uses_immutable_payment_attempt_not_legacy_pending_row():
    tenant_id = uuid4()
    plan_id = uuid4()
    attempt_id = uuid4()
    first_conn = AsyncMock()
    second_conn = AsyncMock()

    @asynccontextmanager
    async def first_context():
        yield first_conn

    @asynccontextmanager
    async def second_context():
        yield second_conn

    session = SimpleNamespace(tenant_id=tenant_id, lifecycle_status="active")
    plan = {
        "id": str(plan_id),
        "name": "Pro",
        "price_annual": 95900,
        "amount_in_cents": 9590000,
    }
    wompi = {"checkout_url": "https://checkout.test", "wompi_link_id": "link-trial"}

    with patch.object(billing, "require_valid_session", return_value=session), patch.object(
        billing, "get_db_connection", side_effect=[first_context(), second_context()]
    ), patch.object(
        billing.billing_service, "tenant_has_trial_subscription", new=AsyncMock(return_value=True)
    ), patch.object(
        billing.billing_service, "get_plan_for_subscribe", new=AsyncMock(return_value=plan)
    ), patch.object(
        billing.legal_service, "ensure_current_terms_accepted", new=AsyncMock()
    ), patch.object(
        billing.billing_service, "create_onboarding_payment_attempt", new=AsyncMock(return_value=attempt_id)
    ) as create_attempt, patch.object(
        billing.billing_service, "attach_onboarding_payment_link", new=AsyncMock()
    ), patch.object(
        billing.billing_service, "subscribe_tenant", new=AsyncMock()
    ) as legacy_subscribe, patch.object(
        billing.wompi_service, "create_payment_link", new=AsyncMock(return_value=wompi)
    ):
        result = await billing.subscribe(
            billing.SubscribeBody(plan_id=plan_id, billing_cycle="annual"),
            SimpleNamespace(),
        )

    create_attempt.assert_awaited_once()
    legacy_subscribe.assert_not_awaited()
    assert result["attempt_id"] == str(attempt_id)


@pytest.mark.asyncio
async def test_trial_cron_fails_closed_without_secret():
    with patch.object(billing.settings, "cron_secret", None):
        with pytest.raises(HTTPException) as exc:
            await billing.process_trial_lifecycle(x_cron_secret=None)

    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_trial_cron_expires_then_sends_warnings():
    expiry_conn = AsyncMock()
    warning_conn = AsyncMock()
    connections = iter([expiry_conn, warning_conn])

    @asynccontextmanager
    async def db_context(*, use_transaction=True):
        conn = next(connections)
        if conn is warning_conn:
            assert use_transaction is False
        yield conn

    with patch.object(billing.settings, "cron_secret", "secret"), patch.object(
        billing, "get_db_connection", side_effect=db_context
    ), patch.object(
        billing.billing_service, "expire_due_trials", new=AsyncMock(return_value=2)
    ) as expire, patch.object(
        billing.billing_email_service,
        "process_trial_warnings",
        new=AsyncMock(return_value={"sent": 1, "skipped": 1, "error": 0}),
    ) as warnings:
        result = await billing.process_trial_lifecycle(x_cron_secret="secret")

    expire.assert_awaited_once_with(expiry_conn)
    warnings.assert_awaited_once_with(warning_conn)
    assert result == {"expired": 2, "sent": 1, "skipped": 1, "error": 0}

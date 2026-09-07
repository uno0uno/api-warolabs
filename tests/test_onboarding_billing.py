"""Paid onboarding checkout and webhook authority (#632)."""
import json
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.routers import billing
from app.services import billing_service, wompi_service


def _attempt_row(
    *, status="pending", price="95900.00", active=True, provider_environment="prod"
):
    attempt_id = uuid4()
    return {
        "id": attempt_id,
        "tenant_id": uuid4(),
        "plan_id": uuid4(),
        "provider_reference": "link-onboarding",
        "expected_amount_in_cents": int(Decimal(price) * 100),
        "currency": "COP",
        "status": status,
        "provider_transaction_id": "tx-approved" if status == "approved" else None,
        "provider_environment": provider_environment,
        "plan_name": "Pro",
        "price_annual": Decimal(price),
        "plan_is_active": active,
    }


def _approved_transaction(attempt):
    return {
        "id": "tx-approved",
        "status": "APPROVED",
        "payment_link_id": attempt["provider_reference"],
        "sku": str(attempt["id"]),
        "amount_in_cents": attempt["expected_amount_in_cents"],
        "currency": "COP",
        "finalized_at": "2026-07-14T12:00:00Z",
    }


@pytest.mark.parametrize(
    ("price", "expected"),
    [(Decimal("95900.00"), 9590000), (Decimal("200000.00"), 20000000)],
)
def test_annual_plan_amount_is_exact_cop(price, expected):
    assert billing_service.annual_price_in_cents(price) == expected


@pytest.mark.parametrize("price", [None, 0, Decimal("1.001"), "NaN"])
def test_annual_plan_rejects_non_billable_price(price):
    with pytest.raises(HTTPException) as exc:
        billing_service.annual_price_in_cents(price)
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_active_plan_catalog_is_server_owned_and_not_hardcoded():
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[
        {
            "id": uuid4(),
            "name": "Pro",
            "slug": "pro",
            "description": None,
            "price_annual": Decimal("95900.00"),
            "features": {"quotas": {}},
        },
        {
            "id": uuid4(),
            "name": "Facturación electrónica",
            "slug": "facturacion-electronica",
            "description": "DIAN",
            "price_annual": Decimal("200000.00"),
            "features": {"electronic_invoice_limit": 200},
        },
    ])

    plans = await billing_service.list_onboarding_plans(conn)

    assert [plan["priceAnnual"] for plan in plans] == [
        Decimal("95900.00"),
        Decimal("200000.00"),
    ]
    assert all(plan["currency"] == "COP" for plan in plans)
    assert "WHERE is_active = true" in conn.fetch.await_args.args[0]


@pytest.mark.asyncio
async def test_inactive_plan_is_rejected_before_checkout():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)

    with pytest.raises(HTTPException) as exc:
        await billing_service.get_plan_for_subscribe(conn, uuid4())

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_each_retry_inserts_a_new_payment_attempt():
    first_id = uuid4()
    second_id = uuid4()
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(side_effect=[{"id": first_id}, {"id": second_id}])
    tenant_id = uuid4()
    plan_id = uuid4()

    first = await billing_service.create_onboarding_payment_attempt(
        conn, tenant_id=tenant_id, plan_id=plan_id, amount_in_cents=9590000
    )
    second = await billing_service.create_onboarding_payment_attempt(
        conn, tenant_id=tenant_id, plan_id=plan_id, amount_in_cents=9590000
    )

    assert first != second
    assert conn.fetchrow.await_count == 2
    for call in conn.fetchrow.await_args_list:
        assert "INSERT INTO billing_payment_attempts" in call.args[0]
        assert "ON CONFLICT" not in call.args[0]
        assert call.args[3] == "lemon_squeezy"
        assert call.args[5] == "COP"
        assert call.args[6] == "prod"


@pytest.mark.asyncio
async def test_create_onboarding_payment_attempt_rejects_wompi():
    conn = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await billing_service.create_onboarding_payment_attempt(
            conn,
            tenant_id=uuid4(),
            plan_id=uuid4(),
            amount_in_cents=9000,
            provider="wompi",
        )
    assert exc.value.status_code == 422
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_payment_status_can_resume_latest_tenant_attempt():
    tenant_id = uuid4()
    attempt = _attempt_row()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={
        "id": attempt["id"],
        "plan_id": attempt["plan_id"],
        "provider_reference": attempt["provider_reference"],
        "provider_transaction_id": None,
        "expected_amount_in_cents": attempt["expected_amount_in_cents"],
        "currency": "COP",
        "status": "pending",
    })

    result = await billing_service.get_onboarding_payment_attempt(
        conn,
        tenant_id=tenant_id,
    )

    assert result["attempt_id"] == attempt["id"]
    query = conn.fetchrow.await_args.args[0]
    assert "WHERE tenant_id = $1" in query
    assert "ORDER BY created_at DESC" in query
    assert conn.fetchrow.await_args.args[1] == tenant_id


@pytest.mark.asyncio
async def test_wompi_link_uses_exact_amount_single_use_and_opaque_attempt_sku():
    attempt_id = uuid4()
    response = MagicMock(status_code=201)
    response.json.return_value = {"data": {"id": "wompi-link"}}
    client = AsyncMock()
    client.post = AsyncMock(return_value=response)
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=client)
    manager.__aexit__ = AsyncMock(return_value=False)

    with patch.object(wompi_service.httpx, "AsyncClient", return_value=manager), patch.object(
        wompi_service, "_headers", return_value={"Authorization": "test"}
    ):
        result = await wompi_service.create_payment_link(
            plan_name="Pro",
            amount_in_cents=9590000,
            billing_cycle="annual",
            sku=attempt_id,
            redirect_url="https://warocol.com/billing/confirmacion",
        )

    payload = client.post.await_args.kwargs["json"]
    assert payload["amount_in_cents"] == 9590000
    assert payload["currency"] == "COP"
    assert payload["single_use"] is True
    assert payload["sku"] == str(attempt_id)
    assert result["wompi_link_id"] == "wompi-link"


def test_missing_wompi_events_secret_fails_closed():
    with patch.object(wompi_service.settings, "wompi_events_secret", None):
        assert wompi_service.verify_event_signature({"environment": "prod"}) is False


@pytest.mark.asyncio
async def test_payment_attempt_environment_mismatch_never_mutates_state():
    attempt = _attempt_row(provider_environment="test")
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=attempt)

    with pytest.raises(HTTPException) as exc:
        await billing_service.process_onboarding_payment_transaction(
            conn,
            _approved_transaction(attempt),
            provider_environment="prod",
        )

    assert exc.value.status_code == 409
    assert "environment" in exc.value.detail.lower()
    conn.execute.assert_not_awaited()
    conn.fetchval.assert_not_awaited()


@pytest.mark.asyncio
async def test_valid_approved_attempt_activates_subscription_once():
    attempt = _attempt_row()
    subscription_id = uuid4()
    period_end = SimpleNamespace(isoformat=lambda: "2027-07-14T12:00:00+00:00")
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[
        attempt,
        None,
        {"id": subscription_id, "current_period_end": period_end},
    ])
    conn.fetchval = AsyncMock(return_value=None)
    identity = {
        "tenant_name": "Restaurante",
        "tenant_email": "owner@example.com",
    }

    with patch.object(
        billing_service.onboarding_service,
        "activate_paid_onboarding_identity",
        new=AsyncMock(return_value=identity),
    ) as activate_identity:
        result = await billing_service.process_onboarding_payment_transaction(
            conn, _approved_transaction(attempt)
        )

    assert result["handled"] is True
    assert result["tenant_info"]["subscription_id"] == str(subscription_id)
    activate_identity.assert_awaited_once_with(conn, attempt["tenant_id"])
    executed_sql = [" ".join(call.args[0].split()) for call in conn.execute.await_args_list]
    assert sum("INSERT INTO billing_events" in sql for sql in executed_sql) == 1
    assert sum("UPDATE billing_payment_attempts" in sql for sql in executed_sql) == 1
    subscription_sql = " ".join(conn.fetchrow.await_args_list[2].args[0].split())
    assert "billing_cycle, status" in subscription_sql
    assert "'annual', 'active'" in subscription_sql


@pytest.mark.asyncio
async def test_duplicate_approved_webhook_does_not_extend_or_emit_event():
    attempt = _attempt_row(status="approved")
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=attempt)
    conn.fetchval = AsyncMock(return_value=None)

    result = await billing_service.process_onboarding_payment_transaction(
        conn, _approved_transaction(attempt)
    )

    assert result == {"handled": True, "tenant_info": None}
    assert conn.fetchrow.await_count == 1
    assert conn.execute.await_count == 1  # advisory lock only


@pytest.mark.asyncio
async def test_transaction_id_cannot_be_reused_by_another_attempt():
    attempt = _attempt_row()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=attempt)
    conn.fetchval = AsyncMock(return_value=uuid4())

    with pytest.raises(HTTPException) as exc:
        await billing_service.process_onboarding_payment_transaction(
            conn, _approved_transaction(attempt)
        )

    assert "transaction ID" in exc.value.detail
    assert "pg_advisory_xact_lock" in conn.execute.await_args_list[0].args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        {"amount_in_cents": 1},
        {"currency": "USD"},
        {"sku": "different-attempt"},
        {"payment_link_id": "different-link"},
    ],
)
async def test_approved_evidence_mismatch_never_activates(override):
    attempt = _attempt_row()
    transaction = _approved_transaction(attempt)
    transaction.update(override)
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=(None if "payment_link_id" in override else attempt))
    conn.fetchval = AsyncMock(return_value=None)

    if "payment_link_id" in override:
        result = await billing_service.process_onboarding_payment_transaction(conn, transaction)
        assert result["handled"] is False
    else:
        with pytest.raises(HTTPException):
            await billing_service.process_onboarding_payment_transaction(conn, transaction)
    assert "INSERT INTO billing_events" not in " ".join(
        " ".join(call.args[0].split()) for call in conn.execute.await_args_list
    )


@pytest.mark.asyncio
async def test_inactive_plan_blocks_approved_activation():
    attempt = _attempt_row(active=False)
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=attempt)
    conn.fetchval = AsyncMock(return_value=None)

    with pytest.raises(HTTPException):
        await billing_service.process_onboarding_payment_transaction(
            conn, _approved_transaction(attempt)
        )


@pytest.mark.asyncio
async def test_late_valid_attempt_after_activation_records_evidence_without_plan_change():
    attempt = _attempt_row(price="200000.00")
    subscription_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[
        attempt,
        {"id": subscription_id, "status": "active"},
    ])
    conn.fetchval = AsyncMock(return_value=None)

    with patch.object(
        billing_service.onboarding_service,
        "activate_paid_onboarding_identity",
        new=AsyncMock(return_value=None),
    ):
        result = await billing_service.process_onboarding_payment_transaction(
            conn, _approved_transaction(attempt)
        )

    assert result == {"handled": True, "tenant_info": None}
    assert conn.fetchrow.await_count == 2
    assert any(
        "UPDATE billing_payment_attempts" in call.args[0]
        for call in conn.execute.await_args_list
    )
    reconciliation = next(
        call for call in conn.execute.await_args_list
        if "payment_reconciliation_required" in call.args[0]
    )
    assert reconciliation.args[2] == subscription_id
    assert reconciliation.args[3] == Decimal("200000.00")
    metadata = json.loads(reconciliation.args[4])
    assert metadata["reason"] == "onboarding_already_activated"
    assert metadata["payment_attempt_id"] == str(attempt["id"])
    assert metadata["requested_plan_id"] == str(attempt["plan_id"])


@pytest.mark.asyncio
async def test_paid_identity_activation_accepts_promoted_admin_and_updates_once():
    tenant_id = uuid4()
    context = {
        "tenant_id": tenant_id,
        "tenant_name": "Restaurante",
        "tenant_email": "owner@example.com",
        "lifecycle_status": "active",
        "onboarding_id": uuid4(),
        "state": "payment_pending",
        "owner_user_id": uuid4(),
        "owner_member_id": uuid4(),
    }
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=context)
    conn.execute = AsyncMock(side_effect=["UPDATE 1", "UPDATE 1", "UPDATE 1"])

    activated = await billing_service.onboarding_service.activate_paid_onboarding_identity(
        conn, tenant_id
    )

    assert activated["tenant_id"] == tenant_id
    identity_query = " ".join(conn.fetchrow.await_args.args[0].split())
    assert "tm.role IN ('owner', 'admin', 'superuser')" in identity_query
    assert conn.execute.await_count == 3
    statements = [" ".join(call.args[0].split()) for call in conn.execute.await_args_list]
    assert "SET is_active = true, role = 'superuser'" in statements[0]
    assert "role IN ('owner', 'admin', 'superuser')" in statements[0]
    assert "SET state = 'active'" in statements[1]
    assert "SET lifecycle_status = 'active'" in statements[2]


@pytest.mark.asyncio
async def test_colombia_wompi_handler_is_noop(caplog):
    """#798 — verified Colombia events must not activate/renew."""
    import logging

    from app.services import wompi_colombia_webhook_service

    with caplog.at_level(logging.WARNING):
        await wompi_colombia_webhook_service.handle_transaction_updated(
            {
                "data": {
                    "transaction": {
                        "id": "tx-approved",
                        "status": "APPROVED",
                        "payment_link_id": "link-onboarding",
                    }
                }
            },
            MagicMock(),
        )

    assert any("deprecated (#798)" in r.message for r in caplog.records)
    assert any(r.levelno == logging.WARNING for r in caplog.records)


@pytest.mark.asyncio
async def test_browser_verify_payment_is_read_only_for_approved_transaction():
    tenant_id = uuid4()
    session = SimpleNamespace(tenant_id=tenant_id)
    transaction = {
        "id": "tx-approved",
        "status": "APPROVED",
        "payment_link_id": "link-onboarding",
        "amount_in_cents": 9590000,
        "currency": "COP",
        "finalized_at": "2026-07-16T18:32:25.077Z",
    }

    @asynccontextmanager
    async def db_context(*args, **kwargs):
        yield AsyncMock()

    with patch.object(billing, "require_valid_session", return_value=session), patch.object(
        billing.wompi_service, "get_transaction", new=AsyncMock(return_value=transaction)
    ), patch.object(billing, "get_db_connection", side_effect=db_context), patch.object(
        billing.billing_service,
        "payment_reference_belongs_to_tenant",
        new=AsyncMock(return_value=True),
    ), patch.object(
        billing.billing_service,
        "activate_tenant_subscription",
        new=AsyncMock(),
    ) as activate:
        result = await billing.verify_payment(
            object(),
            transaction_id="tx-approved",
        )

    assert result["status"] == "active"
    assert result["activation"] == "deprecated"
    activate.assert_not_awaited()


@pytest.mark.asyncio
async def test_browser_verify_payment_does_not_activate_pending_transaction():
    tenant_id = uuid4()
    session = SimpleNamespace(tenant_id=tenant_id)
    transaction = {
        "id": "tx-pending",
        "status": "PENDING",
        "payment_link_id": "link-pending",
        "amount_in_cents": 9590000,
        "currency": "COP",
    }

    @asynccontextmanager
    async def db_context(*args, **kwargs):
        yield AsyncMock()

    with patch.object(billing, "require_valid_session", return_value=session), patch.object(
        billing.wompi_service, "get_transaction", new=AsyncMock(return_value=transaction)
    ), patch.object(billing, "get_db_connection", side_effect=db_context), patch.object(
        billing.billing_service,
        "payment_reference_belongs_to_tenant",
        new=AsyncMock(return_value=True),
    ), patch.object(
        billing.billing_service,
        "activate_tenant_subscription",
        new=AsyncMock(),
    ) as activate:
        result = await billing.verify_payment(
            object(),
            transaction_id="tx-pending",
        )

    assert result["status"] == "pending"
    assert result["activation"] == "deprecated"
    activate.assert_not_awaited()

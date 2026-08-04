"""Direct payables list + AP settlement role coverage (#2110 / #2111 / #2112 / epic #2109)."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import Request

from app.core.middleware import SessionContext
from app.services.account_role_service import AccountRole, MissingAccountRoleError
from app.services.direct_purchase_service import _post_purchase_gl_entry
from app.services.purchase_tracking_service import (
    _post_supplier_payment_gl_entry,
    transition_to_paid,
    validate_state_transition,
)
from app.services.purchases_service import (
    _DIRECT_PAYABLES_SQL,
    _EXCLUDE_DIRECTS_SQL,
    direct_entry_list_clause,
    row_matches_purchases_list_scope,
)


def _session(tenant_id=None, user_id=None):
    return SessionContext({
        "user_id": user_id or uuid4(),
        "tenant_id": tenant_id or uuid4(),
        "email": "ops@warocol.com",
        "name": "Ops",
        "expires_at": None,
        "is_active": True,
        "role": "superuser",
    })


def test_default_list_excludes_directs():
    clause = direct_entry_list_clause(False)
    assert clause == _EXCLUDE_DIRECTS_SQL
    assert "is_direct_entry = FALSE" in clause
    assert "paid_at IS NULL" not in clause


def test_include_direct_payables_allows_unpaid_and_paid_non_contado():
    clause = direct_entry_list_clause(True)
    assert clause == _DIRECT_PAYABLES_SQL
    assert "paid_at IS NULL" in clause
    assert "paid_at IS NOT NULL" in clause
    assert "status = 'received'" in clause
    assert "IS DISTINCT FROM 'contado'" in clause


def test_scope_matrix_default_excludes_all_directs():
    assert row_matches_purchases_list_scope(
        is_direct_entry=False, paid_at=None, status="received",
        payment_type="credito", include_direct_payables=False,
    )
    assert not row_matches_purchases_list_scope(
        is_direct_entry=True, paid_at=None, status="received",
        payment_type="credito", include_direct_payables=False,
    )


def test_scope_matrix_flag_includes_direct_credito_pending_and_paid():
    # unpaid direct crédito received → included (Pendientes)
    assert row_matches_purchases_list_scope(
        is_direct_entry=True, paid_at=None, status="received",
        payment_type="credito", include_direct_payables=True,
    )
    # paid direct crédito → included (Pagadas)
    assert row_matches_purchases_list_scope(
        is_direct_entry=True, paid_at="2026-08-01", status="paid",
        payment_type="credito", include_direct_payables=True,
    )
    # paid contado direct → excluded
    assert not row_matches_purchases_list_scope(
        is_direct_entry=True, paid_at="2026-08-01", status="paid",
        payment_type="contado", include_direct_payables=True,
    )
    # unpaid contado received (shouldn't happen often) → excluded
    assert not row_matches_purchases_list_scope(
        is_direct_entry=True, paid_at=None, status="received",
        payment_type="contado", include_direct_payables=True,
    )
    # classic non-direct still included
    assert row_matches_purchases_list_scope(
        is_direct_entry=False, paid_at=None, status="confirmed",
        payment_type="contado", include_direct_payables=True,
    )


def test_received_to_paid_transition_allowed_for_direct_credito_flow():
    """Pay path for directs uses the same state machine as supplier credit."""
    assert validate_state_transition("received", "paid") is True
    assert validate_state_transition("paid", "received") is False


@pytest.mark.asyncio
async def test_transition_to_paid_direct_received_sets_paid_at_and_checks_quota():
    """Direct credit payables use the same pay path: quota + paid_at UPDATE (no is_direct filter)."""
    tenant_id = uuid4()
    user_id = uuid4()
    purchase_id = uuid4()
    request = MagicMock(spec=Request)
    response = MagicMock()
    session = _session(tenant_id, user_id)

    conn = MagicMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)

    # SELECT purchase — no is_direct_entry filter (directs allowed)
    conn.fetchrow = AsyncMock(
        side_effect=[
            {"id": purchase_id, "status": "received"},
            {
                "purchase_number": "WR-CD-2026-0099",
                "supplier_name": "Proveedor MX",
                "supplier_email": None,
                "supplier_token": None,
                "tenant_site": None,
            },
        ]
    )
    execute_sql: list[str] = []

    async def _execute(sql, *args):
        execute_sql.append(sql)
        return "UPDATE 1"

    conn.execute = AsyncMock(side_effect=_execute)

    @asynccontextmanager
    async def _db_ctx(*_a, **_k):
        yield conn

    quota = AsyncMock()
    gl = AsyncMock()
    resolve_method = AsyncMock(return_value=("cash", SimpleNamespace(id=uuid4())))
    history = AsyncMock()
    attachments = AsyncMock()

    with patch(
        "app.services.purchase_tracking_service.require_valid_session",
        return_value=session,
    ), patch(
        "app.services.purchase_tracking_service.get_db_connection",
        side_effect=_db_ctx,
    ), patch(
        "app.services.purchase_tracking_service._resolve_purchase_payment_account",
        resolve_method,
    ), patch(
        "app.services.purchase_tracking_service.check_plan_quota_period",
        quota,
    ), patch(
        "app.services.purchase_tracking_service.create_status_history_entry",
        history,
    ), patch(
        "app.services.purchase_tracking_service.upload_purchase_attachments",
        attachments,
    ), patch(
        "app.services.purchase_tracking_service._post_supplier_payment_gl_entry",
        gl,
    ), patch(
        "app.services.purchase_tracking_service.discord_purchase_actions_service",
        None,
    ):
        result = await transition_to_paid(
            request,
            response,
            purchase_id,
            payment_method="cash",
            payment_method_id=None,
            payment_reference="REF-1",
            payment_amount=150.0,
            payment_date="2026-08-03T12:00:00Z",
            notes=None,
            files=[],
        )

    assert result["success"] is True
    purchase_select = conn.fetchrow.await_args_list[0].args[0]
    assert "FROM tenant_purchases" in purchase_select
    assert "is_direct_entry" not in purchase_select  # directs not blocked at pay
    update_sql = next(s for s in execute_sql if "UPDATE tenant_purchases" in s)
    assert "paid_at = NOW()" in update_sql
    assert "status = 'paid'" in update_sql
    quota.assert_awaited_once()
    assert quota.await_args.args[1:] == (tenant_id, "supplier_payments_per_period")
    gl.assert_awaited_once()
    assert gl.await_args.kwargs["purchase_id"] == purchase_id
    assert gl.await_args.kwargs["amount"] == 150.0


@pytest.mark.asyncio
async def test_supplier_payment_gl_debits_accounts_payable_role():
    """Settlement journal debits ACCOUNTS_PAYABLE (CxP 2205/2000 via role resolve)."""
    tenant_id = uuid4()
    purchase_id = uuid4()
    ap_id = uuid4()
    cash_id = uuid4()
    entry_id = uuid4()

    conn = MagicMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    conn.fetchval = AsyncMock(side_effect=[None, None])  # no existing GL; period open
    conn.fetchrow = AsyncMock(return_value={"id": entry_id})
    line_args: list[tuple] = []

    async def _execute(sql, *args):
        if "INSERT INTO tenant_journal_lines" in sql:
            line_args.append(args)
        return "INSERT 0 1"

    conn.execute = AsyncMock(side_effect=_execute)

    resolve_account = AsyncMock(
        return_value=SimpleNamespace(id=ap_id, code="2000")
    )
    resolve_method = AsyncMock(
        return_value=("cash", SimpleNamespace(id=cash_id, code="1105"))
    )

    with patch(
        "app.services.purchase_tracking_service.resolve_tenant_timezone",
        AsyncMock(return_value="America/Bogota"),
    ), patch(
        "app.services.purchase_tracking_service.resolve_account",
        resolve_account,
    ), patch(
        "app.services.purchase_tracking_service._resolve_purchase_payment_account",
        resolve_method,
    ):
        await _post_supplier_payment_gl_entry(
            conn=conn,
            tenant_id=tenant_id,
            purchase_id=purchase_id,
            amount=99.5,
            payment_date=datetime(2026, 8, 3, tzinfo=timezone.utc),
            description="Pago proveedor WR-CD-2026-0099",
            payment_method="cash",
            payment_method_id=None,
        )

    resolve_account.assert_awaited_once()
    assert resolve_account.await_args.args[2] == AccountRole.ACCOUNTS_PAYABLE
    assert resolve_account.await_args.kwargs.get("source") == "supplier_payment"
    assert line_args[0][1] == ap_id  # debit AP
    assert line_args[0][2] == 99.5
    assert line_args[1][1] == cash_id  # credit settlement


@pytest.mark.asyncio
async def test_direct_credit_create_gl_resolves_inventory_and_accounts_payable():
    """Credit create posts Dr INVENTORY / Cr AP via AccountRole (no country hardcoding)."""
    tenant_id = uuid4()
    purchase_id = uuid4()
    inv_id = uuid4()
    ap_id = uuid4()
    entry_id = uuid4()

    conn = MagicMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    conn.fetchval = AsyncMock(return_value=None)  # period open
    conn.fetchrow = AsyncMock(return_value={"id": entry_id})
    line_args: list[tuple] = []

    async def _execute(sql, *args):
        if "INSERT INTO tenant_journal_lines" in sql:
            line_args.append(args)
        return "INSERT 0 1"

    conn.execute = AsyncMock(side_effect=_execute)

    async def _resolve(_conn, _tenant_id, role, **kwargs):
        if role == AccountRole.INVENTORY:
            return SimpleNamespace(id=inv_id, code="ROLE-INV")
        if role == AccountRole.ACCOUNTS_PAYABLE:
            return SimpleNamespace(id=ap_id, code="ROLE-AP")
        raise AssertionError(f"unexpected role {role}")

    resolve_account = AsyncMock(side_effect=_resolve)

    with patch(
        "app.services.direct_purchase_service.resolve_account",
        resolve_account,
    ), patch(
        "app.services.direct_purchase_service.local_date_for_tenant",
        return_value=datetime(2026, 8, 3).date(),
    ):
        await _post_purchase_gl_entry(
            conn,
            tenant_id,
            purchase_id,
            total_amount=250.0,
            purchase_date=datetime(2026, 8, 3, tzinfo=timezone.utc),
            description="WR-CD-2026-0100",
            payment_method=None,
            payment_method_id=None,
            timezone_name="America/Bogota",
        )

    assert resolve_account.await_count == 2
    roles = [c.args[2] for c in resolve_account.await_args_list]
    assert roles == [AccountRole.INVENTORY, AccountRole.ACCOUNTS_PAYABLE]
    assert line_args[0][1] == inv_id
    assert line_args[0][2] == 250.0
    assert line_args[1][1] == ap_id
    assert line_args[1][2] == 250.0


def test_missing_account_role_is_explicit_conflict_not_skip():
    """Residual risk: tenant without role binding fails with 409 ACCOUNT_ROLE_MISSING.

    Create (direct_purchase) and pay (transition_to_paid) re-raise MissingAccountRoleError
    rather than silently skipping the journal.
    """
    err = MissingAccountRoleError(uuid4(), AccountRole.ACCOUNTS_PAYABLE, source="supplier_payment")
    assert err.status_code == 409
    assert err.details["code"] == "ACCOUNT_ROLE_MISSING"
    assert err.details["role"] == AccountRole.ACCOUNTS_PAYABLE
    assert "supplier_payment" in err.message or "ACCOUNTS_PAYABLE" in err.message

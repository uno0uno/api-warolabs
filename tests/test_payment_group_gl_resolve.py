"""api-warolabs#782 — payment group GL parents resolve by localization role."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.services.account_role_service import (
    AccountRef,
    AccountRole,
    resolve_group_parent_account,
)
from app.services import payment_method_service


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_global_group_parent_ignores_co_code_and_uses_bank_role():
    """GLOBAL / MX chart: stored 1110 must not win; BANK role → 1010."""
    tenant_id = uuid4()
    bank = AccountRef(uuid4(), "1010", "Bank", AccountRole.BANK, "localization_default")
    conn = MagicMock()

    with patch(
        "app.services.account_role_service.resolve_account_by_id",
        new=AsyncMock(return_value=None),
    ), patch(
        "app.services.account_role_service.resolve_legacy_account",
        new=AsyncMock(),
    ) as legacy, patch(
        "app.services.account_role_service.resolve_account",
        new=AsyncMock(return_value=bank),
    ) as resolve:
        resolved = await resolve_group_parent_account(
            conn,
            tenant_id,
            slug="digital",
            gl_account_id=None,
            gl_account_code="1110",
            group_tenant_id=None,
        )

    assert resolved == bank
    legacy.assert_not_awaited()
    resolve.assert_awaited_once_with(
        conn,
        tenant_id,
        AccountRole.BANK,
        required=False,
        source="payment_group_list",
    )


@pytest.mark.asyncio
async def test_global_group_parent_co_bank_role_returns_puc_1110():
    tenant_id = uuid4()
    bank = AccountRef(uuid4(), "1110", "Bancos", AccountRole.BANK, "localization_default")
    conn = MagicMock()

    with patch(
        "app.services.account_role_service.resolve_account_by_id",
        new=AsyncMock(return_value=None),
    ), patch(
        "app.services.account_role_service.resolve_account",
        new=AsyncMock(return_value=bank),
    ):
        resolved = await resolve_group_parent_account(
            conn,
            tenant_id,
            slug="digital",
            gl_account_code="1110",
            group_tenant_id=None,
        )

    assert resolved is not None
    assert resolved.code == "1110"


@pytest.mark.asyncio
async def test_tenant_group_honors_legacy_code_in_chart():
    tenant_id = uuid4()
    group_tenant_id = tenant_id
    legacy_acct = AccountRef(uuid4(), "9999", "Custom", None, "legacy_binding")
    conn = MagicMock()

    with patch(
        "app.services.account_role_service.resolve_account_by_id",
        new=AsyncMock(return_value=None),
    ), patch(
        "app.services.account_role_service.resolve_legacy_account",
        new=AsyncMock(return_value=legacy_acct),
    ) as legacy, patch(
        "app.services.account_role_service.resolve_account",
        new=AsyncMock(),
    ) as resolve:
        resolved = await resolve_group_parent_account(
            conn,
            tenant_id,
            slug="digital",
            gl_account_code="9999",
            group_tenant_id=group_tenant_id,
        )

    assert resolved == legacy_acct
    legacy.assert_awaited_once()
    resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_groups_exposes_resolved_codes_for_global_digital():
    tenant_id = uuid4()
    digital_id = uuid4()
    bank_id = uuid4()
    conn = MagicMock()
    conn.fetch = AsyncMock(
        return_value=[
            {
                "id": digital_id,
                "tenant_id": None,
                "name": "Digital",
                "slug": "digital",
                "triggers_cartera": False,
                "is_active": True,
                "sort_order": 2,
                "gl_account_code": "1110",
                "gl_account_id": None,
                "method_count": 0,
            }
        ]
    )
    resolved = AccountRef(bank_id, "1010", "Bank", AccountRole.BANK, "localization_default")
    request = MagicMock()

    with patch(
        "app.services.payment_method_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id),
    ), patch(
        "app.services.payment_method_service.get_db_connection",
        return_value=_AsyncContext(conn),
    ), patch(
        "app.services.payment_method_service.resolve_group_parent_account",
        new=AsyncMock(return_value=resolved),
    ) as resolve_parent:
        payload = await payment_method_service.list_groups(request)

    assert payload["success"] is True
    assert payload["data"][0]["glAccountCode"] == "1010"
    assert payload["data"][0]["glAccountId"] == str(bank_id)
    resolve_parent.assert_awaited_once()
    kwargs = resolve_parent.await_args.kwargs
    assert kwargs["slug"] == "digital"
    assert kwargs["gl_account_code"] == "1110"
    assert kwargs["group_tenant_id"] is None


@pytest.mark.asyncio
async def test_list_groups_hides_global_co_code_when_role_unresolved():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetch = AsyncMock(
        return_value=[
            {
                "id": uuid4(),
                "tenant_id": None,
                "name": "Digital",
                "slug": "digital",
                "triggers_cartera": False,
                "is_active": True,
                "sort_order": 2,
                "gl_account_code": "1110",
                "gl_account_id": None,
                "method_count": 0,
            }
        ]
    )
    request = MagicMock()

    with patch(
        "app.services.payment_method_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id),
    ), patch(
        "app.services.payment_method_service.get_db_connection",
        return_value=_AsyncContext(conn),
    ), patch(
        "app.services.payment_method_service.resolve_group_parent_account",
        new=AsyncMock(return_value=None),
    ):
        payload = await payment_method_service.list_groups(request)

    assert payload["data"][0]["glAccountCode"] is None
    assert payload["data"][0]["glAccountId"] is None

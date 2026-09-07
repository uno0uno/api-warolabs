"""GLOBAL chart: reject invalid parent.level+1 by deriving level from code length."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.models.accounting import TenantAccountCreate
from app.services import accounting_service


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _body(**overrides):
    base = dict(
        code="101005",
        name="DAVIPLATA",
        account_class="1",
        account_type="asset",
        normal_balance="debit",
        level=5,  # parent Bank level 4 + 1 — invalid ladder value
        parent_id=uuid4(),
        is_detail=True,
        is_active=True,
        template_id=None,
    )
    base.update(overrides)
    return TenantAccountCreate(**base)


@pytest.mark.asyncio
async def test_global_create_derives_level_when_body_level_invalid():
    tenant_id = uuid4()
    parent_id = uuid4()
    account_id = uuid4()
    conn = MagicMock()
    conn.fetchval = AsyncMock(side_effect=["WARO_HOSPITALITY_GLOBAL_V1", None, True])
    conn.fetchrow = AsyncMock(
        return_value={
            "id": account_id,
            "tenant_id": tenant_id,
            "template_id": None,
            "code": "101005",
            "name": "DAVIPLATA",
            "account_class": "1",
            "account_type": "asset",
            "normal_balance": "debit",
            "level": 6,
            "parent_id": parent_id,
            "is_detail": True,
            "is_system": False,
            "is_active": True,
            "created_at": MagicMock(),
        }
    )
    request = MagicMock()

    with patch(
        "app.services.accounting_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id),
    ), patch(
        "app.services.accounting_service.get_db_connection",
        return_value=_AsyncContext(conn),
    ):
        resp = await accounting_service.create_account(request, _body(parent_id=parent_id))

    assert resp.data.level == 6
    assert resp.data.code == "101005"
    # INSERT uses derived level 6, not body 5
    insert_args = conn.fetchrow.await_args.args
    assert insert_args[8] == 6


@pytest.mark.asyncio
async def test_global_create_keeps_valid_body_level():
    tenant_id = uuid4()
    parent_id = uuid4()
    account_id = uuid4()
    conn = MagicMock()
    conn.fetchval = AsyncMock(side_effect=["WARO_HOSPITALITY_GLOBAL_V1", None, True])
    conn.fetchrow = AsyncMock(
        return_value={
            "id": account_id,
            "tenant_id": tenant_id,
            "template_id": None,
            "code": "101005",
            "name": "DAVIPLATA",
            "account_class": "1",
            "account_type": "asset",
            "normal_balance": "debit",
            "level": 6,
            "parent_id": parent_id,
            "is_detail": True,
            "is_system": False,
            "is_active": True,
            "created_at": MagicMock(),
        }
    )
    request = MagicMock()

    with patch(
        "app.services.accounting_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id),
    ), patch(
        "app.services.accounting_service.get_db_connection",
        return_value=_AsyncContext(conn),
    ):
        await accounting_service.create_account(request, _body(parent_id=parent_id, level=6))

    assert conn.fetchrow.await_args.args[8] == 6


@pytest.mark.asyncio
async def test_derive_level_helpers():
    assert accounting_service._derive_level("1") == 1
    assert accounting_service._derive_level("10") == 2
    assert accounting_service._derive_level("1010") == 4
    assert accounting_service._derive_level("101005") == 6

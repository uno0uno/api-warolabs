"""Unit tests for the permissions resolver (Epic 1 / #E1.4).

The resolver merges per-tenant overrides on top of `DEFAULT_ROLE_MODULES` and
caches results with a TTL. Tests use a fake asyncpg-style connection so they
do not require a live DB.
"""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.core import permissions
from app.core.permissions import (
    DEFAULT_ROLE_MODULES,
    Module,
    Role,
    get_role_modules,
    invalidate_role_modules,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Each test starts with an empty resolver cache."""
    permissions._role_modules_cache.clear()
    yield
    permissions._role_modules_cache.clear()


def _fake_conn(rows):
    """Build a fake asyncpg-style connection that returns `rows` from fetch."""
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows)
    return conn


def _patch_pool(rows):
    """Patch `app.core.permissions.get_db_connection` with an async ctx manager.

    Returns a tuple (patcher, conn) so the test can assert on `conn.fetch`.
    """
    conn = _fake_conn(rows)

    @asynccontextmanager
    async def fake_pool(*_args, **_kwargs):
        yield conn

    return patch.object(permissions, "get_db_connection", fake_pool), conn


class TestOwnerShortcut:
    @pytest.mark.asyncio
    async def test_owner_returns_all_modules_without_db(self):
        patcher, conn = _patch_pool(rows=[])
        with patcher:
            result = await get_role_modules(uuid4(), Role.OWNER)
        assert result == frozenset(Module)
        conn.fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_owner_via_legacy_string_also_short_circuits(self):
        patcher, conn = _patch_pool(rows=[])
        with patcher:
            result = await get_role_modules(uuid4(), "superuser")
        assert result == frozenset(Module)
        conn.fetch.assert_not_called()


class TestDefaultsAndOverrides:
    @pytest.mark.asyncio
    async def test_no_override_rows_returns_defaults(self):
        tenant = uuid4()
        patcher, _conn = _patch_pool(rows=[])
        with patcher:
            result = await get_role_modules(tenant, Role.CASHIER)
        assert result == DEFAULT_ROLE_MODULES[Role.CASHIER]

    @pytest.mark.asyncio
    async def test_grant_adds_module_not_in_defaults(self):
        tenant = uuid4()
        # cashier defaults exclude ABASTECIMIENTO — grant it
        rows = [{"module": Module.ABASTECIMIENTO.value, "granted": True}]
        patcher, _conn = _patch_pool(rows=rows)
        with patcher:
            result = await get_role_modules(tenant, Role.CASHIER)
        assert Module.ABASTECIMIENTO in result
        assert DEFAULT_ROLE_MODULES[Role.CASHIER].issubset(result)

    @pytest.mark.asyncio
    async def test_revoke_removes_module_in_defaults(self):
        tenant = uuid4()
        # cashier defaults include POS — revoke it
        rows = [{"module": Module.POS.value, "granted": False}]
        patcher, _conn = _patch_pool(rows=rows)
        with patcher:
            result = await get_role_modules(tenant, Role.CASHIER)
        assert Module.POS not in result
        assert Module.VENTAS in result  # other defaults preserved

    @pytest.mark.asyncio
    async def test_unknown_module_in_db_is_ignored(self):
        tenant = uuid4()
        rows = [
            {"module": "nonexistent_module", "granted": True},
            {"module": Module.ANALITICA.value, "granted": True},
        ]
        patcher, _conn = _patch_pool(rows=rows)
        with patcher:
            result = await get_role_modules(tenant, Role.CASHIER)
        assert Module.ANALITICA in result
        # No crash; other modules unaffected
        assert Module.POS in result


class TestCache:
    @pytest.mark.asyncio
    async def test_second_call_does_not_hit_db(self):
        tenant = uuid4()
        patcher, conn = _patch_pool(rows=[])
        with patcher:
            await get_role_modules(tenant, Role.CASHIER)
            await get_role_modules(tenant, Role.CASHIER)
        assert conn.fetch.call_count == 1

    @pytest.mark.asyncio
    async def test_different_role_same_tenant_separate_cache_keys(self):
        tenant = uuid4()
        patcher, conn = _patch_pool(rows=[])
        with patcher:
            await get_role_modules(tenant, Role.CASHIER)
            await get_role_modules(tenant, Role.KITCHEN)
        assert conn.fetch.call_count == 2

    @pytest.mark.asyncio
    async def test_invalidate_specific_role_drops_only_that_key(self):
        tenant = uuid4()
        patcher, conn = _patch_pool(rows=[])
        with patcher:
            await get_role_modules(tenant, Role.CASHIER)
            await get_role_modules(tenant, Role.KITCHEN)
            invalidate_role_modules(tenant, Role.CASHIER)
            await get_role_modules(tenant, Role.CASHIER)  # cache miss
            await get_role_modules(tenant, Role.KITCHEN)  # cache hit
        assert conn.fetch.call_count == 3

    @pytest.mark.asyncio
    async def test_invalidate_all_drops_every_role_for_tenant(self):
        tenant_a = uuid4()
        tenant_b = uuid4()
        patcher, conn = _patch_pool(rows=[])
        with patcher:
            await get_role_modules(tenant_a, Role.CASHIER)
            await get_role_modules(tenant_a, Role.KITCHEN)
            await get_role_modules(tenant_b, Role.CASHIER)
            invalidate_role_modules(tenant_a)  # only tenant_a
            await get_role_modules(tenant_a, Role.CASHIER)  # miss
            await get_role_modules(tenant_a, Role.KITCHEN)  # miss
            await get_role_modules(tenant_b, Role.CASHIER)  # still cached
        # 3 initial fills + 2 misses after invalidation = 5
        assert conn.fetch.call_count == 5

    @pytest.mark.asyncio
    async def test_invalidate_with_legacy_string_role(self):
        tenant = uuid4()
        patcher, conn = _patch_pool(rows=[])
        with patcher:
            await get_role_modules(tenant, Role.CASHIER)
            invalidate_role_modules(tenant, "employee")  # legacy → CASHIER
            await get_role_modules(tenant, Role.CASHIER)  # miss
        assert conn.fetch.call_count == 2


class TestLegacyStringInputs:
    @pytest.mark.asyncio
    async def test_legacy_employee_resolves_as_cashier(self):
        tenant = uuid4()
        patcher, _conn = _patch_pool(rows=[])
        with patcher:
            result = await get_role_modules(tenant, "employee")
        assert result == DEFAULT_ROLE_MODULES[Role.CASHIER]

    @pytest.mark.asyncio
    async def test_legacy_member_resolves_as_cashier(self):
        tenant = uuid4()
        patcher, _conn = _patch_pool(rows=[])
        with patcher:
            result = await get_role_modules(tenant, "member")
        assert result == DEFAULT_ROLE_MODULES[Role.CASHIER]

    @pytest.mark.asyncio
    async def test_customer_returns_empty_set(self):
        tenant = uuid4()
        patcher, _conn = _patch_pool(rows=[])
        with patcher:
            result = await get_role_modules(tenant, Role.CUSTOMER)
        assert result == frozenset()

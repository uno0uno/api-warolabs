"""Unit tests for require_module() dependency (Epic 2 / #E2.1).

The dependency reads `tenants.permissions_enforcement_mode` and either
returns silently (allow), logs a `permissions.shadow` event (shadow), or
raises 403 (enforce). Tests use mocked session contexts and a stubbed
DB so they don't need a live tenants table.
"""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.core import permissions
from app.core.permissions import (
    Module,
    Role,
    invalidate_enforcement_mode,
    require_module,
)


# ─── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_caches():
    """Each test starts with empty caches so cross-talk is impossible."""
    permissions._enforcement_mode_cache.clear()
    permissions._role_modules_cache.clear()
    yield
    permissions._enforcement_mode_cache.clear()
    permissions._role_modules_cache.clear()


def _fake_request(session_context):
    """Build a fake FastAPI Request whose state.session_context is set."""
    request = MagicMock()
    request.state.session_context = session_context
    request.url.path = "/api/test"
    return request


class _FakeSession:
    """Minimal SessionContext stand-in. Mirrors the attributes
    `require_module()` reads — keeps tests free of the real middleware."""
    def __init__(self, *, is_valid=True, tenant_id=None, user_id=None, role=None):
        self.is_valid = is_valid
        self.tenant_id = tenant_id or uuid4()
        self.user_id = user_id or uuid4()
        self.role = role


def _patch_get_session(session):
    """Patch `app.core.middleware.get_session_context` to return `session`.

    The dependency imports `get_session_context` lazily inside its body to
    avoid a circular import, so the patch target is the middleware module.
    """
    return patch(
        "app.core.middleware.get_session_context",
        return_value=session,
    )


def _patch_enforcement_mode(mode):
    """Stub the DB call inside `_get_enforcement_mode` to return `mode`."""
    @asynccontextmanager
    async def _ctx():
        conn = MagicMock()
        conn.fetchval = AsyncMock(return_value=mode)
        yield conn
    return patch(
        "app.core.permissions.get_db_connection",
        side_effect=_ctx,
    )


def _patch_role_modules(modules):
    """Stub `get_role_modules` to return `modules` (the resolver's output)."""
    return patch(
        "app.core.permissions.get_role_modules",
        new=AsyncMock(return_value=frozenset(modules)),
    )


# ─── Tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_mode_bypasses_for_denied_role():
    """Mode 'disabled' must allow even when the role lacks the module."""
    dep = require_module(Module.MI_PLAN)
    session = _FakeSession(role="cashier")
    request = _fake_request(session)

    with _patch_get_session(session), _patch_enforcement_mode("disabled"):
        result = await dep(request)

    assert result is None  # silent allow


@pytest.mark.asyncio
async def test_shadow_mode_logs_and_allows_for_denied_role(caplog):
    """Mode 'shadow' logs at WARN and allows the request through."""
    dep = require_module(Module.FINANZAS)
    session = _FakeSession(role="cashier")
    request = _fake_request(session)

    with _patch_get_session(session), \
         _patch_enforcement_mode("shadow"), \
         _patch_role_modules({Module.POS, Module.VENTAS}), \
         caplog.at_level("WARNING", logger="permissions.shadow"):
        result = await dep(request)

    assert result is None
    assert any("would_deny" in rec.message for rec in caplog.records)
    record = next(rec for rec in caplog.records if "would_deny" in rec.message)
    event = record.permission_event
    assert event["role"] == "cashier"
    assert event["module"] == "finanzas"
    assert event["reason"] == "not-in-matrix"


@pytest.mark.asyncio
async def test_enforce_mode_raises_403_for_denied_role():
    """Mode 'enforce' must raise HTTPException(403) when access is denied."""
    dep = require_module(Module.FINANZAS)
    session = _FakeSession(role="cashier")
    request = _fake_request(session)

    with _patch_get_session(session), \
         _patch_enforcement_mode("enforce"), \
         _patch_role_modules({Module.POS, Module.VENTAS}):
        with pytest.raises(HTTPException) as exc_info:
            await dep(request)

    assert exc_info.value.status_code == 403
    assert "finanzas" in exc_info.value.detail


@pytest.mark.asyncio
async def test_allowed_role_passes_in_every_mode():
    """A role that has the module must pass under all three modes."""
    dep = require_module(Module.POS)
    session = _FakeSession(role="cashier")
    request = _fake_request(session)

    for mode in ("disabled", "shadow", "enforce"):
        permissions._enforcement_mode_cache.clear()
        with _patch_get_session(session), \
             _patch_enforcement_mode(mode), \
             _patch_role_modules({Module.POS, Module.VENTAS}):
            result = await dep(request)
        assert result is None, f"mode={mode} should allow"


@pytest.mark.asyncio
async def test_owner_role_short_circuits_via_resolver():
    """Owner is granted everything by `get_role_modules` — dependency allows."""
    dep = require_module(Module.EQUIPO)
    session = _FakeSession(role="owner")
    request = _fake_request(session)

    # Real resolver — owner returns all modules without DB.
    with _patch_get_session(session), _patch_enforcement_mode("enforce"):
        result = await dep(request)

    assert result is None


@pytest.mark.asyncio
async def test_invalid_session_returns_silently():
    """Invalid session → defer to require_valid_session inside the handler."""
    dep = require_module(Module.POS)
    session = _FakeSession(is_valid=False)
    request = _fake_request(session)

    with _patch_get_session(session):
        result = await dep(request)

    assert result is None


@pytest.mark.asyncio
async def test_no_membership_role_is_denied_in_enforce(caplog):
    """User with no role (KDS, API key without membership) → denied/shadow."""
    dep = require_module(Module.POS)
    session = _FakeSession(role=None)
    request = _fake_request(session)

    with _patch_get_session(session), _patch_enforcement_mode("enforce"):
        with pytest.raises(HTTPException) as exc_info:
            await dep(request)
    assert exc_info.value.status_code == 403

    permissions._enforcement_mode_cache.clear()
    with _patch_get_session(session), \
         _patch_enforcement_mode("shadow"), \
         caplog.at_level("WARNING", logger="permissions.shadow"):
        result = await dep(request)
    assert result is None
    assert any(
        getattr(rec, "permission_event", {}).get("reason") == "no-membership"
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_unknown_role_is_denied():
    """A role string outside Role + legacy map → denied (defensive)."""
    dep = require_module(Module.POS)
    session = _FakeSession(role="nonexistent_role_xyz")
    request = _fake_request(session)

    with _patch_get_session(session), _patch_enforcement_mode("enforce"):
        with pytest.raises(HTTPException) as exc_info:
            await dep(request)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_enforcement_mode_cache_avoids_second_db_call():
    """Second call for the same tenant must not hit `get_db_connection`."""
    dep = require_module(Module.POS)
    session = _FakeSession(role="owner")
    request = _fake_request(session)

    with _patch_get_session(session), _patch_enforcement_mode("disabled") as mock_db:
        await dep(request)
        await dep(request)
        # `_patch_enforcement_mode` returns a context manager; each entry calls
        # `get_db_connection` once. We expect exactly ONE call thanks to the cache.
        assert mock_db.call_count == 1


def test_invalidate_enforcement_mode_drops_cached_value():
    """`invalidate_enforcement_mode` must remove the cached entry."""
    tenant_id = uuid4()
    permissions._enforcement_mode_cache[tenant_id] = "enforce"
    assert permissions._enforcement_mode_cache.get(tenant_id) == "enforce"

    invalidate_enforcement_mode(tenant_id)
    assert permissions._enforcement_mode_cache.get(tenant_id) is None

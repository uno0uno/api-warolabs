"""Session idle timeout and concurrent session caps (#823)."""
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.core.platform_superusers import clear_platform_superuser_cache
from app.core.security import IDLE_SESSION_HOURS, INTERNAL_SESSION_HOURS
from app.services.auth_service import replace_active_admin_sessions, session_cap_for_user


def test_idle_and_absolute_constants():
    assert IDLE_SESSION_HOURS == 4
    assert INTERNAL_SESSION_HOURS == 24


@pytest.mark.asyncio
async def test_session_cap_normal_user_is_one(monkeypatch):
    clear_platform_superuser_cache()
    monkeypatch.setattr(
        "app.services.auth_service.is_platform_superuser_email",
        lambda email: False,
    )
    user_id = uuid4()
    conn = AsyncMock()
    conn.fetchval = AsyncMock(side_effect=["user@example.com", None])
    assert await session_cap_for_user(conn, user_id) == 1


@pytest.mark.asyncio
async def test_session_cap_platform_superuser_is_two(monkeypatch):
    clear_platform_superuser_cache()
    monkeypatch.setattr(
        "app.services.auth_service.is_platform_superuser_email",
        lambda email: email == "ops@warocol.com",
    )
    user_id = uuid4()
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value="ops@warocol.com")
    assert await session_cap_for_user(conn, user_id) == 2
    # Should not need tenant_members lookup when platform allowlisted
    assert conn.fetchval.await_count == 1


@pytest.mark.asyncio
async def test_session_cap_tenant_superuser_is_two(monkeypatch):
    clear_platform_superuser_cache()
    monkeypatch.setattr(
        "app.services.auth_service.is_platform_superuser_email",
        lambda email: False,
    )
    user_id = uuid4()
    conn = AsyncMock()
    conn.fetchval = AsyncMock(side_effect=["owner@example.com", 1])
    assert await session_cap_for_user(conn, user_id) == 2


@pytest.mark.asyncio
async def test_replace_keeps_one_other_for_superuser_cap(monkeypatch):
    monkeypatch.setattr(
        "app.services.auth_service.session_cap_for_user",
        AsyncMock(return_value=2),
    )
    user_id = uuid4()
    keep = uuid4()
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="UPDATE 1")
    count = await replace_active_admin_sessions(conn, user_id, keep)
    assert count == 1
    sql, bound_user, bound_keep, keep_others, idle_hours = conn.execute.await_args.args
    assert bound_user == user_id
    assert bound_keep == keep
    assert keep_others == 1  # cap 2 → keep newest 1 other
    assert idle_hours == IDLE_SESSION_HOURS
    assert "ROW_NUMBER()" in sql
    assert "replaced_by_new_login" in sql


@pytest.mark.asyncio
async def test_replace_keeps_zero_others_for_normal_cap(monkeypatch):
    monkeypatch.setattr(
        "app.services.auth_service.session_cap_for_user",
        AsyncMock(return_value=1),
    )
    user_id = uuid4()
    keep = uuid4()
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="UPDATE 2")
    count = await replace_active_admin_sessions(conn, user_id, keep)
    assert count == 2
    _sql, _user, _keep, keep_others, _idle = conn.execute.await_args.args
    assert keep_others == 0


@pytest.mark.asyncio
async def test_get_session_from_request_marks_idle(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from fastapi import Request

    from app.core.security import get_session_from_request

    sid = str(uuid4())
    now = datetime.now(timezone.utc)
    request = Request(
        {
            "type": "http",
            "headers": [(b"cookie", f"session-token={sid}".encode())],
            "method": "GET",
            "path": "/",
        }
    )
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            # session_check
            {
                "id": sid,
                "expires_at": now + timedelta(hours=20),
                "last_activity_at": now - timedelta(hours=5),
                "is_active": True,
                "ended_at": None,
            },
        ]
    )
    conn.execute = AsyncMock()
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=conn)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "app.core.security.get_session_token",
        AsyncMock(return_value=sid),
    ), patch("app.database.get_db_connection", return_value=mock_cm):
        result = await get_session_from_request(request)

    assert result is None
    _sql, token, reason = conn.execute.await_args.args
    assert token == sid
    assert reason == "idle_timeout"

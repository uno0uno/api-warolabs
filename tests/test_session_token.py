"""Session cookie parsing and duplicate-token resolution (#387)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, Request

from app.core.security import collect_session_tokens, get_session_token


def _make_request(cookie_header: str = "", cookies: dict | None = None) -> Request:
    scope = {
        "type": "http",
        "headers": [(b"cookie", cookie_header.encode())] if cookie_header else [],
        "method": "GET",
        "path": "/",
    }
    request = Request(scope)
    if cookies:
        request._cookies = cookies  # type: ignore[attr-defined]
    return request


def test_collect_session_tokens_dedupes_header_values():
    request = _make_request(
        "session-token=aaa; session-token=bbb; session-token=aaa"
    )
    assert collect_session_tokens(request) == ["aaa", "bbb"]


def test_collect_session_tokens_falls_back_to_request_cookies():
    request = _make_request()
    request._cookies = {"session-token": "from-starlette"}  # type: ignore[attr-defined]
    assert collect_session_tokens(request) == ["from-starlette"]


@pytest.mark.asyncio
async def test_get_session_token_picks_newest_valid_among_duplicates():
    request = _make_request("session-token=stale-token; session-token=fresh-token")
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"id": "fresh-token"})
    conn.execute = AsyncMock()

    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=conn)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("app.database.get_db_connection", return_value=mock_cm):
        token = await get_session_token(request)

    assert token == "fresh-token"
    conn.fetchrow.assert_awaited_once()
    assert conn.execute.await_count == 1


@pytest.mark.asyncio
async def test_get_session_token_raises_when_all_invalid():
    request = _make_request("session-token=dead-token")
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock()

    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=conn)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("app.database.get_db_connection", return_value=mock_cm):
        with pytest.raises(HTTPException) as exc:
            await get_session_token(request)

    assert exc.value.status_code == 401

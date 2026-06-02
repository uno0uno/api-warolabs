"""Session cookie parsing and duplicate-token resolution (#387)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, Request

from app.core.security import collect_session_tokens, get_session_token, _normalize_session_token


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
    a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    request = _make_request(
        f"session-token={a}; session-token={b}; session-token={a}"
    )
    assert collect_session_tokens(request) == [a, b]


def test_normalize_session_token_strips_quotes_and_decodes():
    uid = "a37ea75b-1234-5678-9abc-def012345678"
    assert _normalize_session_token(f'"{uid}"') == uid
    assert _normalize_session_token(uid) == uid


def test_collect_session_tokens_ignores_malformed_values():
    request = _make_request("session-token=not-a-uuid; session-token=bbb")
    # bbb is also invalid — should be empty
    assert collect_session_tokens(request) == []


def test_collect_session_tokens_falls_back_to_request_cookies():
    request = _make_request()
    uid = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    request._cookies = {"session-token": uid}  # type: ignore[attr-defined]
    assert collect_session_tokens(request) == [uid]


@pytest.mark.asyncio
async def test_get_session_token_picks_newest_valid_among_duplicates():
    stale = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    fresh = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    request = _make_request(f"session-token={stale}; session-token={fresh}")
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"id": fresh})
    conn.execute = AsyncMock()

    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=conn)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("app.database.get_db_connection", return_value=mock_cm):
        token = await get_session_token(request)

    assert token == fresh
    conn.fetchrow.assert_awaited_once()
    assert conn.execute.await_count == 1


@pytest.mark.asyncio
async def test_get_session_token_raises_when_all_invalid():
    dead = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    request = _make_request(f"session-token={dead}")
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()

    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=conn)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("app.database.get_db_connection", return_value=mock_cm):
        with pytest.raises(HTTPException) as exc:
            await get_session_token(request)

    assert exc.value.status_code == 401
    conn.execute.assert_not_awaited()

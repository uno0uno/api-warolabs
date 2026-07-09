from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services import otp_service


def _profile_row(profile_id=None, email="buyer@example.com", phone_number="3001234567"):
    return {
        "id": profile_id or uuid4(),
        "email": email,
        "phone_number": phone_number,
    }


def _db_context(conn):
    @asynccontextmanager
    async def _ctx():
        yield conn

    return _ctx


def _transaction_context():
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=None)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


@pytest.mark.asyncio
async def test_get_or_create_customer_creates_new_profile_with_real_phone():
    profile_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[None, None, {"id": profile_id}])

    result = await otp_service.get_or_create_customer(
        conn,
        " Buyer@Example.com ",
        "300 123-4567",
    )

    assert result == profile_id
    insert_args = conn.fetchrow.await_args_list[2].args
    assert "INSERT INTO profile" in insert_args[0]
    assert insert_args[1:] == ("buyer@example.com", "3001234567")


@pytest.mark.asyncio
async def test_get_or_create_customer_backfills_blank_phone_for_email_match():
    profile_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[
        _profile_row(profile_id=profile_id, phone_number=""),
        None,
    ])
    conn.execute = AsyncMock()

    result = await otp_service.get_or_create_customer(
        conn,
        "buyer@example.com",
        "300 123-4567",
    )

    assert result == profile_id
    update_args = conn.execute.await_args.args
    assert "UPDATE profile" in update_args[0]
    assert "phone_number = $2" in update_args[0]
    assert update_args[1:] == (profile_id, "3001234567")


@pytest.mark.asyncio
async def test_get_or_create_customer_reuses_phone_match_and_backfills_blank_email():
    profile_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[
        None,
        _profile_row(profile_id=profile_id, email="", phone_number="3001234567"),
    ])
    conn.execute = AsyncMock()

    result = await otp_service.get_or_create_customer(
        conn,
        "Buyer@Example.com",
        "3001234567",
    )

    assert result == profile_id
    update_args = conn.execute.await_args.args
    assert "UPDATE profile" in update_args[0]
    assert "email = $2" in update_args[0]
    assert update_args[1:] == (profile_id, "buyer@example.com")


@pytest.mark.asyncio
async def test_get_or_create_customer_raises_conflict_for_different_email_and_phone_profiles():
    email_profile_id = uuid4()
    phone_profile_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[
        _profile_row(profile_id=email_profile_id, email="buyer@example.com", phone_number=""),
        _profile_row(profile_id=phone_profile_id, email="other@example.com", phone_number="3001234567"),
    ])

    with pytest.raises(HTTPException) as exc_info:
        await otp_service.get_or_create_customer(
            conn,
            "buyer@example.com",
            "3001234567",
        )

    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_get_or_create_customer_preserves_legacy_email_only_creation():
    profile_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[None, {"id": profile_id}])

    result = await otp_service.get_or_create_customer(conn, "Buyer@Example.com")

    assert result == profile_id
    insert_args = conn.fetchrow.await_args_list[1].args
    assert "VALUES ($1, '')" in insert_args[0]
    assert insert_args[1:] == ("buyer@example.com",)


@pytest.mark.asyncio
async def test_verify_otp_code_passes_phone_to_customer_resolution():
    customer_id = uuid4()
    otp_row = {
        "id": uuid4(),
        "otp_code": "123456",
        "is_verified": False,
        "attempts": 0,
        "max_attempts": 3,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    conn = MagicMock()
    conn.transaction = MagicMock(return_value=_transaction_context())
    conn.fetchrow = AsyncMock(return_value=otp_row)
    conn.execute = AsyncMock()

    with patch("app.services.otp_service.get_db_connection", side_effect=_db_context(conn)), \
         patch("app.services.otp_service.get_or_create_customer", new=AsyncMock(return_value=customer_id)) as resolver:
        result = await otp_service.verify_otp_code(
            email=" Buyer@Example.com ",
            cart_id=None,
            otp_code="123456",
            phone_number="300 123-4567",
        )

    assert result["success"] is True
    assert result["customer_id"] == str(customer_id)
    resolver.assert_awaited_once_with(conn, "buyer@example.com", "300 123-4567")

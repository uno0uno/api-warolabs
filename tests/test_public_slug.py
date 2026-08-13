"""Storefront slugify + opaque conflicts + alias redirect — api-warolabs#832."""
from uuid import uuid4

import pytest
from fastapi import HTTPException
from unittest.mock import AsyncMock, patch

from app.core.public_slug import (
    OPAQUE_IDENTITY_CONFLICT,
    is_onboarding_provisional_slug,
    raise_opaque_identity_conflict,
    slugify_business_name,
)
from app.services.public_slug_service import (
    assert_business_identity_available,
    assign_name_based_storefront_slug,
)


def test_slugify_business_name_basic():
    assert slugify_business_name("TEST EEUU 12 06 2026") == "test-eeuu-12-06-2026"
    assert slugify_business_name("  Pizza en leña  ") == "pizza-en-lena"
    assert slugify_business_name("Cafe___Central") == "cafe-central"


def test_slugify_rejects_empty():
    with pytest.raises(HTTPException) as exc:
        slugify_business_name("@@@")
    assert exc.value.status_code == 422


def test_opaque_conflict_has_no_hints():
    with pytest.raises(HTTPException) as exc:
        raise_opaque_identity_conflict()
    assert exc.value.status_code == 409
    assert exc.value.detail == OPAQUE_IDENTITY_CONFLICT
    message = str(exc.value.detail.get("message", "")).lower()
    assert "already" not in message
    assert "taken" not in message
    assert "exists" not in message
    assert "try" not in message


def test_onboarding_provisional_prefix():
    assert is_onboarding_provisional_slug("onboarding-a59cd6a740294f8a")
    assert not is_onboarding_provisional_slug("pizza-en-lena")


@pytest.mark.asyncio
async def test_assert_identity_conflict_on_name():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"id": uuid4()})
    with pytest.raises(HTTPException) as exc:
        await assert_business_identity_available(
            conn,
            business_name="Cafe",
            slug="cafe",
            exclude_tenant_id=None,
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "BUSINESS_IDENTITY_UNAVAILABLE"


@pytest.mark.asyncio
async def test_assign_name_based_stores_onboarding_alias():
    tenant_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            None,  # name free
            None,  # tenant slug free
            None,  # public slug free
            None,  # alias free
            {"slug": "onboarding-aaaaaaaaaaaaaaaa"},
        ]
    )
    conn.execute = AsyncMock(return_value="OK")

    slug = await assign_name_based_storefront_slug(
        conn,
        tenant_id=tenant_id,
        business_name="Cafe Central",
    )
    assert slug == "cafe-central"
    assert any(
        "tenant_public_slug_aliases" in str(c.args[0])
        for c in conn.execute.await_args_list
    )


@pytest.mark.asyncio
async def test_assign_does_not_alias_non_onboarding_previous():
    tenant_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            None,
            None,
            None,
            None,
            {"slug": "old-custom-slug"},
        ]
    )
    conn.execute = AsyncMock(return_value="OK")

    await assign_name_based_storefront_slug(
        conn,
        tenant_id=tenant_id,
        business_name="Cafe Central",
    )
    assert not any(
        "tenant_public_slug_aliases" in str(c.args[0])
        for c in conn.execute.await_args_list
    )


@pytest.mark.asyncio
async def test_slug_moved_response_uses_canonical():
    from app.routers.public_restaurant import _slug_moved_response

    with patch(
        "app.routers.public_restaurant.get_canonical_slug_if_alias",
        new=AsyncMock(return_value="cafe-central"),
    ):
        resp = await _slug_moved_response("onboarding-aaaaaaaaaaaaaaaa")
    assert resp is not None
    assert resp.status_code == 307
    assert resp.body  # JSON body
    assert b"SLUG_MOVED" in resp.body
    assert b"cafe-central" in resp.body

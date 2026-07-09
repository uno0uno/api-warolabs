from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.models.customer import CustomerSearchOrCreate, CustomerUpdate
from app.services import customers_service


def _request():
    return MagicMock()


def _session(tenant_id):
    session = MagicMock()
    session.tenant_id = tenant_id
    return session


def _profile_row(profile_id=None, phone_number="3001234567", email="buyer@example.com"):
    return {
        "id": profile_id or uuid4(),
        "phone_number": phone_number,
        "name": "Buyer",
        "email": email,
        "fiscal_id_type": None,
        "fiscal_id": None,
        "fiscal_business_name": None,
        "fiscal_email": None,
        "created_at": "2026-06-13T00:00:00Z",
        "updated_at": "2026-06-13T00:00:00Z",
    }


def _db_context(conn):
    @asynccontextmanager
    async def _ctx():
        yield conn

    return _ctx


def _patch_customer_service_db(conn, tenant_id):
    return (
        patch("app.services.customers_service.require_valid_session", return_value=_session(tenant_id)),
        patch("app.services.customers_service.get_db_connection", side_effect=_db_context(conn)),
    )


def test_customer_models_normalize_fiscal_id_for_invoicing():
    created = CustomerSearchOrCreate(
        phone_number="3001234567",
        name="Buyer",
        fiscal_id_type="NIT",
        fiscal_id="900.123.456-7",
        fiscal_business_name="ACME SAS",
    )
    updated = CustomerUpdate(
        fiscal_id_type="CC",
        fiscal_id=" 1.063-279-307 ",
        fiscal_business_name="JUAN PEREZ",
    )

    assert created.fiscal_id == "9001234567"
    assert updated.fiscal_id == "1063279307"


@pytest.mark.asyncio
async def test_search_or_create_customer_upserts_customer_relationship_without_touching_team_role():
    tenant_id = uuid4()
    profile_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=_profile_row(profile_id=profile_id))
    conn.execute = AsyncMock(return_value=None)

    session_patch, db_patch = _patch_customer_service_db(conn, tenant_id)
    with session_patch, db_patch:
        result = await customers_service.search_or_create_customer(
            _request(),
            CustomerSearchOrCreate(phone_number="3001234567", name="Buyer"),
        )

    assert result.is_new is False
    assert result.data.id == profile_id
    executed_sql = conn.execute.await_args.args[0]
    assert "INSERT INTO tenant_customers" in executed_sql
    assert "tenant_members" not in executed_sql
    assert conn.execute.await_args.args[1:] == (tenant_id, profile_id)


@pytest.mark.asyncio
async def test_search_or_create_customer_creates_customer_relationship_for_new_profile():
    tenant_id = uuid4()
    profile_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[None, _profile_row(profile_id=profile_id)])
    conn.execute = AsyncMock(return_value=None)

    session_patch, db_patch = _patch_customer_service_db(conn, tenant_id)
    with session_patch, db_patch:
        result = await customers_service.search_or_create_customer(
            _request(),
            CustomerSearchOrCreate(phone_number="3007654321", name="New Buyer"),
        )

    assert result.is_new is True
    customer_upsert_sql = conn.execute.await_args.args[0]
    assert "INSERT INTO tenant_customers" in customer_upsert_sql
    assert "tenant_members" not in customer_upsert_sql
    assert conn.execute.await_args.args[1:] == (tenant_id, profile_id)


@pytest.mark.asyncio
async def test_get_customer_by_id_uses_tenant_customers_as_source_of_truth():
    tenant_id = uuid4()
    profile_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=_profile_row(profile_id=profile_id))

    session_patch, db_patch = _patch_customer_service_db(conn, tenant_id)
    with session_patch, db_patch:
        result = await customers_service.get_customer_by_id(_request(), profile_id)

    assert result.data.id == profile_id
    query = conn.fetchrow.await_args.args[0]
    assert "tenant_customers tc" in query
    assert "tenant_members" not in query
    assert "tc.is_active = true" in query


@pytest.mark.asyncio
async def test_search_customers_by_query_uses_tenant_customers():
    tenant_id = uuid4()
    profile_id = uuid4()
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[
        {
            "id": profile_id,
            "name": "Buyer",
            "phone_number": "3001234567",
            "email": "buyer@example.com",
            "fiscal_id": None,
            "fiscal_id_type": None,
        }
    ])

    session_patch, db_patch = _patch_customer_service_db(conn, tenant_id)
    with session_patch, db_patch:
        result = await customers_service.search_customers_by_query(_request(), "Buyer")

    assert result.data[0].id == profile_id
    query = conn.fetch.await_args.args[0]
    assert "tenant_customers tc" in query
    assert "tenant_members" not in query
    assert "tc.is_active = true" in query


@pytest.mark.asyncio
async def test_update_customer_requires_customer_relationship_not_team_role():
    tenant_id = uuid4()
    profile_id = uuid4()
    updated_row = _profile_row(profile_id=profile_id, phone_number="3009999999")
    updated_row["name"] = "Updated Buyer"
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[{"id": profile_id}, updated_row])

    session_patch, db_patch = _patch_customer_service_db(conn, tenant_id)
    with session_patch, db_patch:
        result = await customers_service.update_customer(
            _request(),
            profile_id,
            CustomerUpdate(name="Updated Buyer"),
        )

    assert result.data.name == "Updated Buyer"
    membership_query = conn.fetchrow.await_args_list[0].args[0]
    assert "tenant_customers tc" in membership_query
    assert "tenant_members" not in membership_query
    assert "tc.is_active = true" in membership_query

"""
Integration tests for GET /online/orders/{order_id}/status-history endpoint.

Tests the status timeline retrieval, authentication, and error cases.

Real DB anchors (warocolombia tenant):
  tenant_id : 93b3e582-34fa-44a6-8d0f-bf82a3608727

Note: Because these tests run against the real DB without an authenticated
session (no valid session cookie), unauthenticated requests return 401.
The 404 logic is verified by monkeypatching require_valid_session so the
service layer is exercised with a valid session.

For the happy-path test we fetch a real online order, add a status transition
via the PATCH endpoint (to ensure history rows exist), then verify the GET
response, and finally roll back the order status to leave the DB clean.
"""
import pytest
from httpx import AsyncClient
from uuid import uuid4
from datetime import datetime, timedelta
from unittest.mock import patch

from app.database import DatabasePool
from app.core.middleware import SessionContext

TENANT_ID = "93b3e582-34fa-44a6-8d0f-bf82a3608727"

VALID_SESSION_DATA = {
    "user_id": None,  # nullable — avoids UUID cast failure for non-UUID test IDs
    "tenant_id": TENANT_ID,
    "email": "test@warocol.com",
    "name": "Test User",
    "expires_at": datetime.now() + timedelta(days=7),
    "is_active": True,
}


@pytest.fixture(autouse=True)
async def reset_db_pool():
    """Reset the asyncpg pool after each test (event-loop isolation)."""
    yield
    if DatabasePool._pool is not None:
        try:
            await DatabasePool._pool.close()
        except Exception:
            pass
        DatabasePool._pool = None


def _mock_session_context() -> SessionContext:
    return SessionContext(VALID_SESSION_DATA)


class TestGetStatusHistoryUnauthenticated:
    """Requests without a valid session must return 401."""

    @pytest.mark.asyncio
    async def test_get_status_history_unauthenticated(self, client: AsyncClient):
        """GET without session cookie returns 401."""
        fake_order_id = str(uuid4())
        response = await client.get(
            f"/online/orders/{fake_order_id}/status-history",
        )
        assert response.status_code == 401


class TestGetStatusHistoryNotFound:
    """Order lookup with a non-existent UUID returns 404."""

    @pytest.mark.asyncio
    async def test_get_status_history_not_found(self, auth_client: AsyncClient):
        """Random UUID that doesn't exist in DB returns 404."""
        nonexistent_order_id = str(uuid4())
        with patch(
            "app.services.online_orders_service.require_valid_session",
            return_value=_mock_session_context(),
        ):
            response = await auth_client.get(
                f"/online/orders/{nonexistent_order_id}/status-history",
            )
        assert response.status_code == 404


class TestGetStatusHistoryHappyPath:
    """Happy-path: returns list of history rows sorted by change_date ASC."""

    @pytest.mark.asyncio
    async def test_get_status_history_returns_rows(self, auth_client: AsyncClient):
        """
        After a PATCH transition (pending → confirmed), the status history
        endpoint returns at least one row with the correct shape.

        We roll back the transition at the end (confirmed → cancelled).
        If no pending order exists, the test is skipped.
        """
        from app.database import get_db_connection

        async with get_db_connection(use_transaction=False) as conn:
            row = await conn.fetchrow(
                """
                SELECT id FROM orders
                WHERE tenant_id = $1
                  AND status = 'pending'
                  AND online_cart_id IS NOT NULL
                LIMIT 1
                """,
                TENANT_ID,
            )

        if not row:
            pytest.skip("No pending online orders found in test DB")

        order_id = str(row["id"])

        # Create a history row via the PATCH endpoint
        with patch(
            "app.services.online_orders_service.require_valid_session",
            return_value=_mock_session_context(),
        ):
            patch_response = await auth_client.patch(
                f"/online/orders/{order_id}/status",
                json={"new_status": "confirmed", "reason": "test history"},
            )
        assert patch_response.status_code == 200

        # Now fetch the history
        with patch(
            "app.services.online_orders_service.require_valid_session",
            return_value=_mock_session_context(),
        ):
            response = await auth_client.get(
                f"/online/orders/{order_id}/status-history",
            )

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert isinstance(body["data"], list)
        assert len(body["data"]) >= 1

        # Find the first entry (oldest) — old_status should be None (initial creation)
        # or at minimum the list is ordered ASC by change_date
        first = body["data"][0]
        assert "id" in first
        assert "old_status" in first
        assert "new_status" in first
        assert "change_date" in first
        assert "reason" in first

        # The last entry in the list should be our confirmed transition
        last = body["data"][-1]
        assert last["new_status"] == "confirmed"

        # Verify ordering: change_dates should be non-decreasing
        dates = [entry["change_date"] for entry in body["data"]]
        assert dates == sorted(dates)

        # Rollback: confirmed → cancelled to leave DB in a clean state
        with patch(
            "app.services.online_orders_service.require_valid_session",
            return_value=_mock_session_context(),
        ):
            await auth_client.patch(
                f"/online/orders/{order_id}/status",
                json={"new_status": "cancelled", "reason": "test rollback"},
            )

    @pytest.mark.asyncio
    async def test_get_status_history_empty_returns_200(self, auth_client: AsyncClient):
        """
        An order with no history rows returns 200 with an empty data list.

        We find an online order, delete its history rows temporarily,
        verify the response, then restore them.
        If no online order exists, the test is skipped.
        """
        from app.database import get_db_connection

        async with get_db_connection(use_transaction=False) as conn:
            row = await conn.fetchrow(
                """
                SELECT id FROM orders
                WHERE tenant_id = $1
                  AND online_cart_id IS NOT NULL
                LIMIT 1
                """,
                TENANT_ID,
            )

        if not row:
            pytest.skip("No online orders found in test DB")

        order_id = row["id"]

        # Temporarily remove all history for this order
        async with get_db_connection() as conn:
            deleted_rows = await conn.fetch(
                """
                DELETE FROM order_status_history
                WHERE order_id = $1
                RETURNING id, old_status, new_status, change_date, changed_by, reason, created_at
                """,
                order_id,
            )

        with patch(
            "app.services.online_orders_service.require_valid_session",
            return_value=_mock_session_context(),
        ):
            response = await auth_client.get(
                f"/online/orders/{str(order_id)}/status-history",
            )

        # Restore the deleted rows
        if deleted_rows:
            async with get_db_connection() as conn:
                for r in deleted_rows:
                    await conn.execute(
                        """
                        INSERT INTO order_status_history
                            (id, order_id, old_status, new_status, change_date, changed_by, reason, created_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                        """,
                        r["id"], order_id, r["old_status"], r["new_status"],
                        r["change_date"], r["changed_by"], r["reason"], r["created_at"],
                    )

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"] == []

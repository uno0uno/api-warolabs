"""
Integration tests for PATCH /online/orders/{order_id}/status endpoint.

Tests the order status state machine, history recording, and error cases.

Real DB anchors (warocolombia tenant):
  tenant_id : 93b3e582-34fa-44a6-8d0f-bf82a3608727
  All current online orders have status = 'pending' — safe to use any real order UUID
  for 404 tests we use uuid4() which will not exist.

Note: Because these tests run against the real DB without an authenticated
session (no valid session cookie), all PATCH requests return 401.
The unauthenticated 401 case is verified explicitly.
The 400/404 logic is verified by mocking the session context on request.state.

For state machine and 404 logic tests, we use monkeypatching of
require_valid_session so the service layer is exercised with a valid session.
"""
import pytest
from httpx import AsyncClient
from uuid import uuid4
from datetime import datetime, timedelta
from unittest.mock import patch, AsyncMock

from app.database import DatabasePool
from app.core.middleware import SessionContext

TENANT_ID = "93b3e582-34fa-44a6-8d0f-bf82a3608727"
# A real pending order UUID in the test DB — used for happy-path and invalid transition tests
# We'll fetch it dynamically in fixtures.

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


class TestUpdateOrderStatusUnauthenticated:
    """Requests without a valid session must return 401."""

    @pytest.mark.asyncio
    async def test_unauthenticated_returns_401(self, client: AsyncClient):
        """PATCH without session cookie returns 401."""
        fake_order_id = str(uuid4())
        response = await client.patch(
            f"/online/orders/{fake_order_id}/status",
            json={"new_status": "confirmed"},
        )
        assert response.status_code == 401


class TestUpdateOrderStatusValidation:
    """Pydantic validation on the request body."""

    @pytest.mark.asyncio
    async def test_invalid_status_value_returns_422(self, client: AsyncClient):
        """Sending a status not in the Literal enum returns 422 from Pydantic."""
        fake_order_id = str(uuid4())
        response = await client.patch(
            f"/online/orders/{fake_order_id}/status",
            json={"new_status": "flying"},
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_new_status_returns_422(self, client: AsyncClient):
        """Body without new_status is rejected by Pydantic."""
        fake_order_id = str(uuid4())
        response = await client.patch(
            f"/online/orders/{fake_order_id}/status",
            json={"reason": "testing"},
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_order_id_returns_422(self, client: AsyncClient):
        """Non-UUID path parameter is rejected by FastAPI."""
        response = await client.patch(
            "/online/orders/not-a-uuid/status",
            json={"new_status": "confirmed"},
        )
        assert response.status_code == 422


class TestUpdateOrderStatusNotFound:
    """Order lookup with a non-existent UUID returns 404."""

    @pytest.mark.asyncio
    async def test_order_not_found_returns_404(self, auth_client: AsyncClient):
        """Random UUID that doesn't exist in DB returns 404."""
        nonexistent_order_id = str(uuid4())
        with patch(
            "app.services.online_orders_service.require_valid_session",
            return_value=_mock_session_context(),
        ):
            response = await auth_client.patch(
                f"/online/orders/{nonexistent_order_id}/status",
                json={"new_status": "confirmed"},
            )
        assert response.status_code == 404


class TestUpdateOrderStatusStateMachine:
    """State machine transition enforcement."""

    @pytest.mark.asyncio
    async def test_valid_transition_pending_to_confirmed(self, auth_client: AsyncClient):
        """
        Happy path: pending → confirmed on a real pending order.

        We fetch a real pending online order from the DB, perform the transition,
        then immediately roll it back (confirmed → cancelled) to leave DB clean.
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

        with patch(
            "app.services.online_orders_service.require_valid_session",
            return_value=_mock_session_context(),
        ):
            response = await auth_client.patch(
                f"/online/orders/{order_id}/status",
                json={"new_status": "confirmed", "reason": "test confirmation"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data"]["old_status"] == "pending"
        assert data["data"]["new_status"] == "confirmed"
        assert data["data"]["order_id"] == order_id
        assert "changed_at" in data["data"]

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
    async def test_invalid_transition_pending_to_delivered_returns_400(
        self, auth_client: AsyncClient
    ):
        """pending → delivered is not in ALLOWED_TRANSITIONS → 400."""
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

        with patch(
            "app.services.online_orders_service.require_valid_session",
            return_value=_mock_session_context(),
        ):
            response = await auth_client.patch(
                f"/online/orders/{order_id}/status",
                json={"new_status": "delivered"},
            )

        assert response.status_code == 400
        body = response.json()
        assert "Invalid transition" in body["message"]

    @pytest.mark.asyncio
    async def test_terminal_state_completed_returns_400(self, auth_client: AsyncClient):
        """
        An order whose status is 'completed' (terminal) cannot be transitioned.
        We seed a completed-status order using a direct DB UPDATE, test it,
        then restore the original status.
        """
        from app.database import get_db_connection

        async with get_db_connection(use_transaction=False) as conn:
            row = await conn.fetchrow(
                """
                SELECT id, status FROM orders
                WHERE tenant_id = $1
                  AND online_cart_id IS NOT NULL
                LIMIT 1
                """,
                TENANT_ID,
            )

        if not row:
            pytest.skip("No online orders found in test DB")

        order_id = row["id"]
        original_status = row["status"]

        # Force status to 'completed' directly in DB for this test
        async with get_db_connection() as conn:
            await conn.execute(
                "UPDATE orders SET status = 'completed', updated_at = NOW() WHERE id = $1",
                order_id,
            )

        with patch(
            "app.services.online_orders_service.require_valid_session",
            return_value=_mock_session_context(),
        ):
            response = await auth_client.patch(
                f"/online/orders/{str(order_id)}/status",
                json={"new_status": "confirmed"},
            )

        # Restore original status
        async with get_db_connection() as conn:
            await conn.execute(
                "UPDATE orders SET status = $1, updated_at = NOW() WHERE id = $2",
                original_status,
                order_id,
            )

        assert response.status_code == 400
        body = response.json()
        assert "Invalid transition" in body["message"]
        assert "terminal" in body["message"]

    @pytest.mark.asyncio
    async def test_terminal_state_cancelled_returns_400(self, auth_client: AsyncClient):
        """An order whose status is 'cancelled' (terminal) cannot be transitioned."""
        from app.database import get_db_connection

        async with get_db_connection(use_transaction=False) as conn:
            row = await conn.fetchrow(
                """
                SELECT id, status FROM orders
                WHERE tenant_id = $1
                  AND online_cart_id IS NOT NULL
                LIMIT 1
                """,
                TENANT_ID,
            )

        if not row:
            pytest.skip("No online orders found in test DB")

        order_id = row["id"]
        original_status = row["status"]

        async with get_db_connection() as conn:
            await conn.execute(
                "UPDATE orders SET status = 'cancelled', updated_at = NOW() WHERE id = $1",
                order_id,
            )

        with patch(
            "app.services.online_orders_service.require_valid_session",
            return_value=_mock_session_context(),
        ):
            response = await auth_client.patch(
                f"/online/orders/{str(order_id)}/status",
                json={"new_status": "confirmed"},
            )

        # Restore
        async with get_db_connection() as conn:
            await conn.execute(
                "UPDATE orders SET status = $1, updated_at = NOW() WHERE id = $2",
                original_status,
                order_id,
            )

        assert response.status_code == 400
        body = response.json()
        assert "Invalid transition" in body["message"]
        assert "terminal" in body["message"]

    @pytest.mark.asyncio
    async def test_optional_reason_accepted(self, auth_client: AsyncClient):
        """Request without 'reason' field is accepted (reason is optional)."""
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

        with patch(
            "app.services.online_orders_service.require_valid_session",
            return_value=_mock_session_context(),
        ):
            response = await auth_client.patch(
                f"/online/orders/{order_id}/status",
                json={"new_status": "confirmed"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["data"]["reason"] is None

        # Rollback
        with patch(
            "app.services.online_orders_service.require_valid_session",
            return_value=_mock_session_context(),
        ):
            await auth_client.patch(
                f"/online/orders/{order_id}/status",
                json={"new_status": "cancelled", "reason": "test rollback"},
            )


class TestAutoComplete:
    """Tests for auto_complete flag on pending → confirmed transition."""

    @pytest.mark.asyncio
    async def test_auto_complete_pending_to_completed(self, auth_client: AsyncClient):
        """
        auto_complete=True on pending → confirmed should leave the order as
        completed and produce two history rows.
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

        with patch(
            "app.services.online_orders_service.require_valid_session",
            return_value=_mock_session_context(),
        ):
            response = await auth_client.patch(
                f"/online/orders/{order_id}/status",
                json={"new_status": "confirmed", "auto_complete": True},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data"]["new_status"] == "completed"
        assert data["data"]["old_status"] == "pending"
        assert data["data"]["auto_completed"] is True
        assert len(data["data"]["transitions"]) == 2
        assert data["data"]["transitions"][0]["from"] == "pending"
        assert data["data"]["transitions"][0]["to"] == "confirmed"
        assert data["data"]["transitions"][1]["from"] == "confirmed"
        assert data["data"]["transitions"][1]["to"] == "completed"

        # Verify DB state
        from app.database import get_db_connection
        async with get_db_connection(use_transaction=False) as conn:
            db_row = await conn.fetchrow(
                "SELECT status FROM orders WHERE id = $1", row["id"]
            )
            history_rows = await conn.fetch(
                """
                SELECT old_status, new_status FROM order_status_history
                WHERE order_id = $1
                ORDER BY change_date ASC
                """,
                row["id"],
            )

        assert db_row["status"] == "completed"
        transitions = [(r["old_status"], r["new_status"]) for r in history_rows]
        assert ("pending", "confirmed") in transitions
        assert ("confirmed", "completed") in transitions

        # Rollback: force back to pending for other tests
        async with get_db_connection() as conn:
            await conn.execute(
                "UPDATE orders SET status = 'pending', updated_at = NOW() WHERE id = $1",
                row["id"],
            )

    @pytest.mark.asyncio
    async def test_auto_complete_false_leaves_confirmed(self, auth_client: AsyncClient):
        """
        auto_complete=False (default) on pending → confirmed should leave the
        order as confirmed — unchanged from existing behavior.
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

        with patch(
            "app.services.online_orders_service.require_valid_session",
            return_value=_mock_session_context(),
        ):
            response = await auth_client.patch(
                f"/online/orders/{order_id}/status",
                json={"new_status": "confirmed", "auto_complete": False},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data"]["new_status"] == "confirmed"
        assert data["data"].get("auto_completed") is not True

        # Rollback: confirmed → cancelled to leave DB in a clean state
        with patch(
            "app.services.online_orders_service.require_valid_session",
            return_value=_mock_session_context(),
        ):
            await auth_client.patch(
                f"/online/orders/{order_id}/status",
                json={"new_status": "cancelled", "reason": "test rollback"},
            )

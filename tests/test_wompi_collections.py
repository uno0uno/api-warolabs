"""Restaurant Wompi collections (#862)."""
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.core.exceptions import NotFoundError, ValidationError
from app.services import wompi_collections_service as svc
from app.services.account_role_service import AccountRef, AccountRole


SQL = Path("sql/20260817_wompi_collections.sql")


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _request(tenant_id):
    req = MagicMock()
    req.state = MagicMock()
    return req, tenant_id


@pytest.fixture
def tenant_id():
    return uuid4()


def test_sql_is_additive_create_only():
    sql = SQL.read_text()
    assert "CREATE TABLE IF NOT EXISTS tenant_wompi_merchants" in sql
    assert "CREATE TABLE IF NOT EXISTS tenant_wompi_collection_sessions" in sql
    assert "DROP TABLE" not in sql.upper()
    assert "DROP COLUMN" not in sql.upper()


def test_next_puc_child_prefers_suffix_05():
    assert svc.next_puc_child_code("1110", {"1110"}) == "111005"


def test_next_puc_child_skips_taken_05():
    assert svc.next_puc_child_code("1110", {"1110", "111005"}) == "111001"


def test_reject_server_merchant_keys(monkeypatch):
    monkeypatch.setattr(svc.settings, "wompi_public_key", "pub_prod_waro")
    monkeypatch.setattr(svc.settings, "wompi_private_key", "prv_prod_waro")
    assert svc._matches_server_merchant("pub_prod_waro", "prv_test_rest") is True
    assert svc._matches_server_merchant("pub_test_rest", "prv_prod_waro") is True
    assert svc._matches_server_merchant("pub_test_rest", "prv_test_rest") is False


def test_collections_never_call_server_wompi_headers():
    src = Path("app/services/wompi_collections_service.py").read_text()
    assert "wompi_service._headers" not in src
    assert "settings.wompi_private_key" in src  # only to reject match
    router = Path("app/routers/wompi_collections.py").read_text()
    assert "payments_webhook" not in router
    assert '@session_router.get("/sessions/{session_id}")' in router
    assert "require_module" not in router.split('@session_router.get("/sessions/{session_id}")', 1)[1].split("@session_router.post")[0]
    assert '@session_router.post("/sessions/online")' in router
    online_block = router.split('@session_router.post("/sessions/online")', 1)[1].split("@session_router.", 1)[0]
    assert "require_module" not in online_block


@pytest.mark.asyncio
async def test_activate_fails_without_digitales_parent(tenant_id):
    req, _ = _request(tenant_id)
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "id": uuid4(),
            "tenant_id": None,
            "gl_account_id": None,
            "gl_account_code": "1110",
        }
    )
    with patch(
        "app.services.wompi_collections_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id),
    ), patch(
        "app.services.wompi_collections_service._matches_server_merchant",
        return_value=False,
    ), patch(
        "app.services.wompi_collections_service.validate_merchant_keys",
        new=AsyncMock(),
    ), patch(
        "app.services.wompi_collections_service.openbao_transit.encrypt_plaintext",
        new=AsyncMock(side_effect=["ct-prv", "ct-evt"]),
    ), patch(
        "app.services.wompi_collections_service.get_db_connection",
        return_value=_AsyncContext(conn),
    ), patch(
        "app.services.wompi_collections_service.resolve_group_parent_account",
        new=AsyncMock(return_value=None),
    ):
        with pytest.raises(ValidationError, match="cuenta padre"):
            await svc.activate_merchant(
                req,
                public_key="pub_test_rest",
                private_key="prv_test_rest",
                events_secret="evt",
            )


@pytest.mark.asyncio
async def test_activate_creates_puc_child_and_method(tenant_id):
    req, _ = _request(tenant_id)
    group_id = uuid4()
    parent = AccountRef(uuid4(), "1110", "Bancos", AccountRole.BANK, "localization_default")
    method_id = uuid4()
    account_id = uuid4()
    conn = MagicMock()

    async def fetchrow(sql, *args):
        if "payment_method_groups" in sql:
            return {
                "id": group_id,
                "tenant_id": None,
                "gl_account_id": parent.id,
                "gl_account_code": "1110",
            }
        if "FROM payment_methods" in sql:
            return None
        if "INSERT INTO tenant_accounts" in sql:
            return {"id": account_id, "code": args[1]}
        if "INSERT INTO payment_methods" in sql:
            return {"id": method_id}
        return None

    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    conn.fetch = AsyncMock(return_value=[{"code": "1110"}])
    conn.execute = AsyncMock()

    with patch(
        "app.services.wompi_collections_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id),
    ), patch(
        "app.services.wompi_collections_service._matches_server_merchant",
        return_value=False,
    ), patch(
        "app.services.wompi_collections_service.validate_merchant_keys",
        new=AsyncMock(),
    ), patch(
        "app.services.wompi_collections_service.openbao_transit.encrypt_plaintext",
        new=AsyncMock(side_effect=["ct-prv", "ct-evt"]),
    ), patch(
        "app.services.wompi_collections_service.get_db_connection",
        return_value=_AsyncContext(conn),
    ), patch(
        "app.services.wompi_collections_service.resolve_group_parent_account",
        new=AsyncMock(return_value=parent),
    ):
        result = await svc.activate_merchant(
            req,
            public_key="pub_test_restXXXX",
            private_key="prv_test_rest",
            events_secret="evt",
        )

    assert result["success"] is True
    assert result["data"]["glAccountCode"] == "111005"
    assert result["data"]["paymentMethodId"] == str(method_id)
    assert "fingerprint" in result["data"]


@pytest.mark.asyncio
async def test_generic_customer_when_none_selected(tenant_id):
    generic_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"id": generic_id})
    found = await svc.resolve_collection_customer(conn, tenant_id, None)
    assert found == generic_id
    assert conn.fetchrow.await_args.args[2] == "0000000000"


@pytest.mark.asyncio
async def test_selected_customer_must_belong_to_tenant(tenant_id):
    selected = uuid4()
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=1)
    found = await svc.resolve_collection_customer(conn, tenant_id, selected)
    assert found == selected


@pytest.mark.asyncio
async def test_selected_customer_rejected_if_foreign(tenant_id):
    selected = uuid4()
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=None)
    with pytest.raises(ValidationError, match="no pertenece"):
        await svc.resolve_collection_customer(conn, tenant_id, selected)


@pytest.mark.asyncio
async def test_idempotent_apply_webhook_then_get(tenant_id):
    session_id = uuid4()
    order_id = uuid4()
    method_id = uuid4()
    payment_id = uuid4()
    session_row = {
        "id": session_id,
        "order_id": order_id,
        "amount": Decimal("15000"),
        "status": "pending",
        "order_payment_id": None,
    }
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "tenant_id": tenant_id,
                "payment_method_id": method_id,
                "is_active": True,
                "private_key_ciphertext": "x",
                "events_secret_ciphertext": "y",
                "environment": "test",
            },
            {"id": payment_id},
        ]
    )
    conn.execute = AsyncMock()

    first = await svc.apply_approved_payment(
        conn,
        tenant_id=tenant_id,
        session_row=session_row,
        provider_tx_id="tx-1",
    )
    assert first["applied"] is True
    assert first["idempotent"] is False

    approved_row = {
        **session_row,
        "status": "approved",
        "order_payment_id": payment_id,
    }
    second = await svc.apply_approved_payment(
        conn,
        tenant_id=tenant_id,
        session_row=approved_row,
        provider_tx_id="tx-1",
    )
    assert second["applied"] is False
    assert second["idempotent"] is True
    assert second["orderPaymentId"] == str(payment_id)


@pytest.mark.asyncio
async def test_idempotent_apply_get_then_webhook(tenant_id):
    payment_id = uuid4()
    session_row = {
        "id": uuid4(),
        "order_id": uuid4(),
        "amount": Decimal("2000"),
        "status": "approved",
        "order_payment_id": payment_id,
    }
    conn = MagicMock()
    result = await svc.apply_approved_payment(
        conn,
        tenant_id=tenant_id,
        session_row=session_row,
        provider_tx_id="tx-2",
    )
    assert result["idempotent"] is True
    conn.fetchrow.assert_not_called()


@pytest.mark.asyncio
async def test_public_session_returns_only_checkout_url_and_status():
    session_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "checkout_url": "https://checkout.wompi.co/l/abc",
            "status": "pending",
        }
    )
    with patch.object(svc, "get_db_connection", return_value=_AsyncContext(conn)):
        result = await svc.public_collection_session(session_id)
    assert set(result["data"].keys()) == {"checkoutUrl", "status"}
    assert result["data"]["checkoutUrl"] == "https://checkout.wompi.co/l/abc"
    assert "key" not in str(result["data"]).lower()
    assert "secret" not in str(result).lower()


@pytest.mark.asyncio
async def test_online_session_rejects_mismatched_cart():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "id": uuid4(),
            "tenant_id": uuid4(),
            "customer_id": uuid4(),
            "online_cart_id": uuid4(),
        }
    )
    with patch.object(svc, "get_db_connection", return_value=_AsyncContext(conn)):
        with pytest.raises(NotFoundError, match="Orden no encontrada"):
            await svc.create_online_collection_session(
                order_id=uuid4(),
                cart_id=uuid4(),
                amount=Decimal("15000"),
            )

"""Restaurant Wompi collections (#862)."""
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
import json

import pytest

from app.core.exceptions import NotFoundError, ValidationError
from app.services import wompi_collections_service as svc
from app.services.account_role_service import AccountRef, AccountRole


SQL = Path("sql/20260817_wompi_collections.sql")
PENDING_SQL = Path("sql/20260818_wompi_one_pending_session.sql")


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


def test_rename_migration_is_additive_and_provider_agnostic():
    """#894 — provider-agnostic rename + payload columns."""
    from pathlib import Path
    sql = Path("sql/20260820_payment_provider_rename_and_payload.sql").read_text()
    assert "RENAME TO tenant_payment_providers" in sql
    assert "RENAME TO tenant_collection_sessions" in sql
    assert "ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'wompi'" in sql
    assert "ADD COLUMN IF NOT EXISTS provider_payload JSONB" in sql
    assert "ADD COLUMN IF NOT EXISTS provider_payment_method_type TEXT" in sql
    assert "ADD COLUMN IF NOT EXISTS customer_email TEXT" in sql
    assert "ADD COLUMN IF NOT EXISTS currency TEXT" in sql
    assert "ADD COLUMN IF NOT EXISTS environment TEXT" in sql
    # backfill safety net
    assert "provider = 'wompi'" in sql
    # the migration is additive: no DROP, no TRUNCATE
    upper = sql.upper()
    assert "DROP TABLE" not in upper
    assert "DROP COLUMN" not in upper
    assert "TRUNCATE" not in upper


def test_pending_session_unique_index_is_additive():
    sql = PENDING_SQL.read_text()
    assert "CREATE UNIQUE INDEX IF NOT EXISTS tenant_wompi_collection_sessions_pending_order_uidx" in sql
    assert "WHERE status = 'pending'" in sql
    assert "DROP TABLE" not in sql.upper()
    assert "DROP COLUMN" not in sql.upper()


def test_next_puc_child_prefers_suffix_05():
    assert svc.next_puc_child_code("1110", {"1110"}) == "111005"


def test_next_puc_child_skips_taken_05():
    assert svc.next_puc_child_code("1110", {"1110", "111005"}) == "111001"


def test_merchant_public_keys_from_object_or_list():
    assert svc.merchant_public_keys_from_wompi_body(
        {"data": {"public_key": "pub_test_a"}}
    ) == ["pub_test_a"]
    assert svc.merchant_public_keys_from_wompi_body(
        {"data": [{"public_key": "pub_test_b"}, {"id": 1}]}
    ) == ["pub_test_b"]
    assert svc.merchant_public_keys_from_wompi_body({"data": []}) == []


def test_payment_link_id_from_object_or_list():
    assert svc.payment_link_id_from_wompi_body({"data": {"id": "test_abc"}}) == "test_abc"
    assert svc.payment_link_id_from_wompi_body({"data": [{"id": "test_list"}]}) == "test_list"
    assert svc.payment_link_id_from_wompi_body({"data": []}) is None


def test_wompi_redirect_follows_local_and_prod_origin():
    sid = uuid4()
    local = f"http://localhost:8080/cobro/{sid}/gracias"
    prod = f"https://warocol.com/cobro/{sid}/gracias"
    assert svc._wompi_payment_link_redirect(local, sid) == local
    assert svc._wompi_payment_link_redirect(prod, sid) == prod
    assert svc.wompi_resource_data({"data": {"id": "tx"}}) == {"id": "tx"}
    assert svc.wompi_resource_data({"data": [{"id": "tx"}]}) == [{"id": "tx"}]


@pytest.mark.asyncio
async def test_validate_merchant_keys_accepts_list_data():
    pub = "pub_test_ok"
    payload = {"data": [{"id": 1}, {"public_key": pub}]}
    response = MagicMock(status_code=200)
    response.json.return_value = payload
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    with patch("app.services.wompi_collections_service.httpx.AsyncClient", return_value=client):
        await svc.validate_merchant_keys(pub, "prv_test_ok", "test")


def test_collections_never_call_server_wompi_headers():
    src = Path("app/services/wompi_collections_service.py").read_text()
    assert "wompi_service._headers" not in src
    assert "settings.wompi_private_key" not in src
    assert "_matches_server_merchant" not in src
    router = Path("app/routers/wompi_collections.py").read_text()
    assert "payments_webhook" not in router
    assert '@session_router.get("/sessions/{session_id}")' in router
    assert "require_module" not in router.split('@session_router.get("/sessions/{session_id}")', 1)[1].split("@session_router.post")[0]
    assert '@session_router.get("/sessions", dependencies=[Depends(require_any_module(Module.POS, Module.VENTAS))])' in router
    assert '@session_router.post("/sessions", dependencies=[Depends(require_any_module(Module.POS, Module.VENTAS))])' in router
    assert '@session_router.post("/sessions/regenerate", dependencies=[Depends(require_any_module(Module.POS, Module.VENTAS))])' in router
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
    assert "tc.profile_id" in conn.fetchrow.await_args.args[0]
    assert "tc.customer_id" not in conn.fetchrow.await_args.args[0]


@pytest.mark.asyncio
async def test_selected_customer_must_belong_to_tenant(tenant_id):
    selected = uuid4()
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=1)
    found = await svc.resolve_collection_customer(conn, tenant_id, selected)
    assert found == selected
    sql = conn.fetchval.await_args.args[0]
    assert "profile_id" in sql
    assert "customer_id" not in sql


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
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()

    with patch.object(svc, "_post_approved_collection_gl", new=AsyncMock()) as gl:
        first = await svc.apply_approved_payment(
            conn,
            tenant_id=tenant_id,
            session_row=session_row,
            provider_tx_id="tx-1",
        )
    gl.assert_awaited_once()
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
    """#894 — public /gracias lookup now exposes provider + payload fields, no secrets."""
    session_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "checkout_url": "https://checkout.wompi.co/l/abc",
            "status": "approved",
            "provider": "wompi",
            "provider_payment_method_type": "CARD",
            "customer_email": "diner@example.com",
            "currency": "COP",
            "environment": "test",
            "provider_payload": {"id": "tx-1", "status": "APPROVED", "amount_in_cents": 5100000},
        }
    )
    with patch.object(svc, "get_db_connection", return_value=_AsyncContext(conn)):
        result = await svc.public_collection_session(session_id)
    data = result["data"]
    assert data["checkoutUrl"] == "https://checkout.wompi.co/l/abc"
    assert data["status"] == "approved"
    assert data["provider"] == "wompi"
    assert data["providerPaymentMethodType"] == "CARD"
    assert data["customerEmail"] == "diner@example.com"
    assert data["currency"] == "COP"
    assert data["environment"] == "test"
    assert data["providerPayload"]["id"] == "tx-1"
    # no secret-like keys in the public response
    flat = str(data).lower()
    assert "private_key" not in flat
    assert "ciphertext" not in flat
    assert "events_secret" not in flat
    assert "integrity_secret" not in flat


@pytest.mark.asyncio
async def test_public_session_returns_minimal_when_columns_null():
    """#894 — backfill-safe: rows from before the migration have null new columns; no error."""
    session_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "checkout_url": "https://checkout.wompi.co/l/abc",
            "status": "pending",
            "provider": "wompi",
            "provider_payment_method_type": None,
            "customer_email": None,
            "currency": None,
            "environment": None,
            "provider_payload": None,
        }
    )
    with patch.object(svc, "get_db_connection", return_value=_AsyncContext(conn)):
        result = await svc.public_collection_session(session_id)
    data = result["data"]
    assert data["provider"] == "wompi"
    assert data["providerPaymentMethodType"] is None
    assert data["providerPayload"] is None


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
                amount=Decimal("1"),
            )


def test_safe_thank_you_url_allowlist():
    session_id = uuid4()
    default = f"https://warocol.com/cobro/{session_id}/gracias"
    assert svc._safe_thank_you_url(None, session_id) == default
    assert svc._safe_thank_you_url(
        "https://warocol.com/cobro/{sessionId}/gracias", session_id
    ) == default
    assert svc._safe_thank_you_url("https://evil.example/phish", session_id) == default


@pytest.mark.asyncio
async def test_apply_skips_when_order_already_paid(tenant_id):
    session_row = {
        "id": uuid4(),
        "order_id": uuid4(),
        "amount": Decimal("15000"),
        "status": "pending",
        "order_payment_id": None,
    }
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=1)
    conn.execute = AsyncMock()
    result = await svc.apply_approved_payment(
        conn,
        tenant_id=tenant_id,
        session_row=session_row,
        provider_tx_id="tx-dup",
    )
    assert result["idempotent"] is True
    conn.fetchrow.assert_not_called()
    assert conn.execute.await_count == 2


def test_pick_referenced_transaction_prefers_approved():
    rows = [
        {"id": "tx-declined", "status": "DECLINED"},
        {"id": "tx-ok", "status": "APPROVED"},
    ]
    picked = svc._pick_referenced_transaction(rows)
    assert picked["id"] == "tx-ok"
    assert svc._pick_referenced_transaction([]) is None


@pytest.mark.asyncio
async def test_staff_session_returns_only_id_and_status(tenant_id):
    req, _ = _request(tenant_id)
    order_id = uuid4()
    session_id = uuid4()
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=1)
    conn.fetchrow = AsyncMock(return_value={"id": session_id, "status": "pending"})
    with patch(
        "app.services.wompi_collections_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id),
    ), patch(
        "app.services.wompi_collections_service.get_db_connection",
        return_value=_AsyncContext(conn),
    ):
        result = await svc.staff_session_for_order(req, order_id)
    assert set(result["data"].keys()) == {"id", "status"}
    assert result["data"]["id"] == str(session_id)
    assert "checkout" not in str(result).lower()
    assert "key" not in str(result["data"]).lower()


@pytest.mark.asyncio
async def test_create_session_reuses_pending(tenant_id):
    req, _ = _request(tenant_id)
    order_id = uuid4()
    session_id = uuid4()
    customer_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {"id": order_id, "customer_id": customer_id, "total_amount": Decimal("10")},
            {"id": session_id, "status": "pending", "customer_id": customer_id},
        ]
    )
    with patch(
        "app.services.wompi_collections_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id),
    ), patch(
        "app.services.wompi_collections_service.get_db_connection",
        return_value=_AsyncContext(conn),
    ), patch(
        "app.services.wompi_collections_service.resolve_collection_customer",
        new=AsyncMock(return_value=customer_id),
    ), patch.object(
        svc, "_create_session_row", new=AsyncMock()
    ) as create_row:
        result = await svc.create_collection_session(
            req, order_id=order_id, amount=Decimal("10")
        )
    create_row.assert_not_called()
    assert result["data"]["id"] == str(session_id)
    assert result["data"]["status"] == "pending"
    assert "checkoutUrl" not in result["data"]


@pytest.mark.asyncio
async def test_regenerate_expires_pending_then_creates(tenant_id):
    req, _ = _request(tenant_id)
    order_id = uuid4()
    old_id = uuid4()
    new_id = uuid4()
    customer_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {"id": order_id, "customer_id": customer_id, "total_amount": Decimal("10")},
            {"id": old_id, "status": "pending", "customer_id": customer_id},
        ]
    )
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    created = {
        "success": True,
        "data": {"id": str(new_id), "status": "pending", "customerId": str(customer_id)},
    }
    with patch(
        "app.services.wompi_collections_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id),
    ), patch(
        "app.services.wompi_collections_service.get_db_connection",
        return_value=_AsyncContext(conn),
    ), patch(
        "app.services.wompi_collections_service.resolve_collection_customer",
        new=AsyncMock(return_value=customer_id),
    ), patch.object(
        svc, "_create_session_row", new=AsyncMock(return_value=created)
    ) as create_row:
        result = await svc.regenerate_collection_session(
            req, order_id=order_id, amount=Decimal("10")
        )
    create_row.assert_awaited_once()
    assert conn.execute.await_args.args[1] == old_id
    assert "expired" in conn.execute.await_args.args[0]
    assert result["data"]["id"] == str(new_id)


@pytest.mark.asyncio
async def test_regenerate_rejects_when_approved(tenant_id):
    req, _ = _request(tenant_id)
    order_id = uuid4()
    customer_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={"id": order_id, "customer_id": customer_id, "total_amount": Decimal("10")}
    )
    conn.fetchval = AsyncMock(return_value=1)
    with patch(
        "app.services.wompi_collections_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id),
    ), patch(
        "app.services.wompi_collections_service.get_db_connection",
        return_value=_AsyncContext(conn),
    ), patch.object(
        svc, "_create_session_row", new=AsyncMock()
    ) as create_row:
        with pytest.raises(ValidationError, match="ya fue aprobado"):
            await svc.regenerate_collection_session(
                req, order_id=order_id, amount=Decimal("10")
            )
    create_row.assert_not_called()


@pytest.mark.asyncio
async def test_verify_looks_up_transaction_by_reference(tenant_id):
    session_id = uuid4()
    session_row = {
        "id": session_id,
        "tenant_id": tenant_id,
        "order_id": uuid4(),
        "status": "pending",
        "provider_tx_id": None,
        "order_payment_id": None,
        "amount": Decimal("5000"),
    }
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            session_row,
            {
                "tenant_id": tenant_id,
                "payment_method_id": uuid4(),
                "is_active": True,
                "private_key_ciphertext": "ct",
                "events_secret_ciphertext": "evt",
                "environment": "test",
            },
        ]
    )
    with patch.object(svc, "get_db_connection", return_value=_AsyncContext(conn)), patch(
        "app.services.wompi_collections_service.openbao_transit.decrypt_ciphertext",
        new=AsyncMock(return_value="prv"),
    ), patch.object(
        svc, "fetch_transaction_by_reference", new=AsyncMock(return_value=None)
    ) as lookup, patch.object(
        svc, "fetch_transaction", new=AsyncMock()
    ) as by_id:
        result = await svc.verify_session(session_id)
    by_id.assert_not_called()
    lookup.assert_awaited_once()
    assert result["data"]["applied"] is False
    assert result["data"]["status"] == "pending"


# --- #894 provider payload persistence ------------------------------------

@pytest.mark.asyncio
async def test_apply_from_transaction_persists_payload_for_approved(tenant_id):
    """#894 — APPROVED transaction: provider_payload + provider_payment_method_type +
    customer_email + currency + environment must be persisted on the session row.
    """
    session_id = uuid4()
    order_id = uuid4()
    method_id = uuid4()
    payment_id = uuid4()
    session_row = {
        "id": session_id,
        "order_id": order_id,
        "amount": Decimal("51000"),
        "status": "pending",
        "order_payment_id": None,
    }
    transaction = {
        "id": "tx-1894-A",
        "status": "APPROVED",
        "amount_in_cents": 5100000,
        "payment_method_type": "CARD",
        "customer_email": "diner@example.com",
        "currency": "COP",
        "environment": "test",
        "status_message": "Transacción aprobada",
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
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()

    with patch.object(svc, "_post_approved_collection_gl", new=AsyncMock()):
        result = await svc.apply_from_transaction(
            conn,
            tenant_id=tenant_id,
            session_row=session_row,
            transaction=transaction,
        )

    assert result["applied"] is True
    # Find the UPDATE that wrote the session to approved (the last execute on the session)
    final_session_update = None
    for call in conn.execute.await_args_list:
        sql = call.args[0] if call.args else ""
        if "UPDATE tenant_collection_sessions" in sql and "status = 'approved'" in sql:
            final_session_update = call
            break
    assert final_session_update is not None, "expected final approved UPDATE on tenant_collection_sessions"
    # positional args after the SQL: (session_id, provider_tx_id, payment_id, payload_json_str, pm_type, email, currency, env)
    args = final_session_update.args[1:]
    assert args[0] == session_id
    assert args[1] == "tx-1894-A"
    assert args[2] == payment_id
    payload = args[3]
    # #896 fix: asyncpg JSONB expects a JSON-encoded string, not a dict
    assert isinstance(payload, str)
    decoded = json.loads(payload)
    assert decoded["id"] == "tx-1894-A"
    assert decoded["status"] == "APPROVED"
    assert args[4] == "CARD"
    assert args[5] == "diner@example.com"
    assert args[6] == "COP"
    assert args[7] == "test"


@pytest.mark.asyncio
async def test_apply_from_transaction_persists_payload_for_declined(tenant_id):
    """#894 — DECLINED transaction: status + provider_tx_id + payload + provider fields
    must be persisted, but the order is not marked paid.
    """
    session_id = uuid4()
    session_row = {
        "id": session_id,
        "order_id": uuid4(),
        "amount": Decimal("1000"),
        "status": "pending",
        "order_payment_id": None,
    }
    transaction = {
        "id": "tx-1894-D",
        "status": "DECLINED",
        "amount_in_cents": 100000,
        "payment_method_type": "CARD",
        "customer_email": "nope@example.com",
        "currency": "COP",
        "environment": "test",
        "status_message": "Fondos insuficientes",
    }
    conn = MagicMock()
    conn.execute = AsyncMock()

    result = await svc.apply_from_transaction(
        conn,
        tenant_id=tenant_id,
        session_row=session_row,
        transaction=transaction,
    )

    assert result["applied"] is False
    assert result["status"] == "DECLINED"

    update_call = conn.execute.await_args
    sql = update_call.args[0]
    assert "UPDATE tenant_collection_sessions" in sql
    assert "provider_payload" in sql
    assert "provider_payment_method_type" in sql
    assert "customer_email" in sql
    assert "currency" in sql
    assert "environment" in sql
    args = update_call.args[1:]
    assert args[0] == session_id
    assert args[1] == "declined"
    assert args[2] == "tx-1894-D"
    # #896 fix: payload is a JSON-encoded string, not a dict
    assert isinstance(args[3], str)
    decoded = json.loads(args[3])
    assert decoded["id"] == "tx-1894-D"
    assert args[4] == "CARD"
    assert args[5] == "nope@example.com"
    assert args[6] == "COP"


@pytest.mark.asyncio
async def test_apply_from_transaction_persists_payload_for_error(tenant_id):
    """#894 — ERROR (network / voided) also stores the payload for audit."""
    session_id = uuid4()
    session_row = {
        "id": session_id,
        "order_id": uuid4(),
        "amount": Decimal("1000"),
        "status": "pending",
        "order_payment_id": None,
    }
    transaction = {
        "id": "tx-1894-E",
        "status": "ERROR",
        "amount_in_cents": 100000,
        "payment_method_type": "BANCOLOMBIA_TRANSFER",
        "currency": "COP",
        "environment": "test",
    }
    conn = MagicMock()
    conn.execute = AsyncMock()

    result = await svc.apply_from_transaction(
        conn,
        tenant_id=tenant_id,
        session_row=session_row,
        transaction=transaction,
    )

    assert result["applied"] is False
    assert result["status"] == "ERROR"
    args = conn.execute.await_args.args[1:]
    assert args[1] == "error"
    assert args[2] == "tx-1894-E"
    assert args[4] == "BANCOLOMBIA_TRANSFER"


def test_jsonable_coerces_decimal_datetime_uuid_and_nested():
    from datetime import datetime, timezone
    from uuid import uuid4
    uid = uuid4()
    src = {
        "amount": Decimal("51000.50"),
        "ts": datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc),
        "uid": uid,
        "items": [{"price": Decimal("10"), "tags": ["a", "b"]}],
        "null_field": None,
        "plain": "hello",
    }
    out = svc._jsonable(src)
    assert out["amount"] == "51000.50"
    assert out["ts"] == "2026-08-20T12:00:00+00:00"
    assert out["uid"] == str(uid)
    assert out["items"][0]["price"] == "10"
    assert out["items"][0]["tags"] == ["a", "b"]
    assert out["null_field"] is None
    assert out["plain"] == "hello"


@pytest.mark.asyncio
async def test_provider_payload_is_json_encoded_string_for_asyncpg(tenant_id):
    """#896 — asyncpg JSONB expects a JSON-encoded string, not a dict. Regress if we
    drop the json.dumps() wrap in either UPDATE call site.
    """
    session_id = uuid4()
    order_id = uuid4()
    method_id = uuid4()
    payment_id = uuid4()
    transaction = {
        "id": "tx-1896",
        "status": "APPROVED",
        "amount_in_cents": 1234500,
        "payment_method_type": "CARD",
        "customer_email": "qa@example.com",
        "currency": "COP",
        "environment": "test",
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
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()

    with patch.object(svc, "_post_approved_collection_gl", new=AsyncMock()):
        await svc.apply_from_transaction(
            conn,
            tenant_id=tenant_id,
            session_row={
                "id": session_id,
                "order_id": order_id,
                "amount": Decimal("12345"),
                "status": "pending",
                "order_payment_id": None,
            },
            transaction=transaction,
        )

    # Every UPDATE on tenant_collection_sessions that writes provider_payload
    # must use a JSON-encoded string at the provider_payload positional argument.
    payload_writes = [
        call for call in conn.execute.await_args_list
        if "UPDATE tenant_collection_sessions" in call.args[0]
        and "provider_payload" in call.args[0]
    ]
    assert payload_writes, "expected at least one UPDATE on tenant_collection_sessions with provider_payload"
    for call in payload_writes:
        # the $4 placeholder for provider_payload — find it by looking for the JSONB arg
        sql = call.args[0]
        assert "provider_payload = $4" in sql or "provider_payload = COALESCE($4" in sql
        arg = call.args[4]  # $4 = provider_payload
        assert isinstance(arg, str), f"provider_payload must be a JSON string, got {type(arg).__name__}"
        # round-trip is valid JSON
        parsed = json.loads(arg)
        assert "id" in parsed or "status" in parsed


@pytest.mark.asyncio
async def test_apply_approved_payment_with_payload_serializes_to_string(tenant_id):
    """#896 — apply_approved_payment called directly must also send a JSON string."""
    session_id = uuid4()
    order_id = uuid4()
    method_id = uuid4()
    payment_id = uuid4()
    payload = {
        "id": "tx-1896-DIRECT",
        "status": "APPROVED",
        "amount_in_cents": 5000_00,
        "payment_method_type": "BANCOLOMBIA_TRANSFER",
        "customer_email": "direct@example.com",
        "currency": "COP",
        "environment": "test",
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
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()

    with patch.object(svc, "_post_approved_collection_gl", new=AsyncMock()):
        await svc.apply_approved_payment(
            conn,
            tenant_id=tenant_id,
            session_row={
                "id": session_id,
                "order_id": order_id,
                "amount": Decimal("5000"),
                "status": "pending",
                "order_payment_id": None,
            },
            provider_tx_id="tx-1896-DIRECT",
            provider_payload=payload,
        )

    # Find the final UPDATE on tenant_collection_sessions (not SELECT pg_notify, not UPDATE orders).
    session_updates = [
        call for call in conn.execute.await_args_list
        if "UPDATE tenant_collection_sessions" in call.args[0]
        and "status = 'approved'" in call.args[0]
    ]
    assert session_updates, "expected final approved UPDATE on tenant_collection_sessions"
    final_update = session_updates[-1]
    assert "provider_payload = COALESCE($4" in final_update.args[0]
    payload_arg = final_update.args[4]
    assert isinstance(payload_arg, str)
    decoded = json.loads(payload_arg)
    assert decoded["id"] == "tx-1896-DIRECT"

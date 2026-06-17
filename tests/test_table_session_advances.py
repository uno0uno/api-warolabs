"""Tests for table-session minimum consumption advances (warocol.com#1370)."""
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.exceptions import APIError
from app.services import table_session_advances_service as svc


class _AsyncContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _DbContext:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeConn:
    def __init__(self):
        self.tenant_id = uuid4()
        self.user_id = uuid4()
        self.table_id = uuid4()
        self.session_id = uuid4()
        self.advance_id = uuid4()
        self.payment_group_id = uuid4()
        self.payment_method_id = uuid4()
        self.fetchrow_queries = []
        self.fetch_queries = []
        self.execute_queries = []
        self.advance_row = {
            "id": self.advance_id,
            "tenant_id": self.tenant_id,
            "table_session_id": self.session_id,
            "amount_cop": Decimal("50000.00"),
            "payment_method": "cash",
            "payment_method_id": self.payment_method_id,
            "journal_entry_id": None,
            "void_journal_entry_id": None,
            "status": "active",
            "notes": "cover",
            "void_reason": None,
            "voided_at": None,
            "created_at": None,
        }

    def transaction(self):
        return _AsyncContext()

    async def fetchrow(self, query, *args):
        self.fetchrow_queries.append(query)
        compact = " ".join(query.split())
        if "FROM tables" in compact:
            return {"id": self.table_id}
        if "FROM table_sessions" in compact:
            return {"id": self.session_id, "table_id": self.table_id}
        if "FROM payment_method_groups" in compact:
            return {"id": self.payment_group_id}
        if "FROM payment_methods" in compact and "WHERE id = $1" in compact:
            return {"id": self.payment_method_id}
        if "INSERT INTO table_session_advances" in compact:
            return self.advance_row.copy()
        if "idempotency_key" in compact:
            return None
        if "FROM tenant_accounts" in compact:
            return None
        return None

    async def fetch(self, query, *args):
        self.fetch_queries.append(query)
        compact = " ".join(query.split())
        if "FROM table_session_advances" in compact:
            return [self.advance_row.copy()]
        return []

    async def execute(self, query, *args):
        self.execute_queries.append(query)
        return "OK"


def _patch_request_context(monkeypatch, conn):
    monkeypatch.setattr(
        svc,
        "require_valid_session",
        lambda request: SimpleNamespace(
            tenant_id=str(conn.tenant_id),
            user_id=str(conn.user_id),
        ),
    )
    monkeypatch.setattr(svc, "get_db_connection", lambda *args, **kwargs: _DbContext(conn))


def _all_queries(conn):
    return "\n".join(conn.fetchrow_queries + conn.fetch_queries + conn.execute_queries)


class TestAdvanceValidation:
    def test_rejects_customer_wallet_tender(self):
        with pytest.raises(APIError) as exc:
            svc._validate_advance_tender("customer_wallet")
        assert exc.value.status_code == 400

    def test_rejects_non_positive_amount(self):
        with pytest.raises(APIError) as exc:
            svc._amount_decimal(0)
        assert exc.value.status_code == 400

    def test_active_and_voided_totals_stay_separate(self):
        rows = [
            {"amount_cop": Decimal("10"), "status": "active"},
            {"amount_cop": Decimal("20"), "status": "voided"},
        ]
        assert svc._advance_totals(rows) == {
            "active_total_cop": 10.0,
            "available_total_cop": 10.0,
            "applied_total_cop": 0.0,
            "voided_total_cop": 20.0,
        }

    def test_applied_amount_reduces_available_total(self):
        rows = [
            {"amount_cop": Decimal("50"), "applied_amount_cop": Decimal("30"), "status": "active"},
            {"amount_cop": Decimal("20"), "applied_amount_cop": Decimal("0"), "status": "active"},
        ]
        assert svc._advance_totals(rows) == {
            "active_total_cop": 40.0,
            "available_total_cop": 40.0,
            "applied_total_cop": 30.0,
            "voided_total_cop": 0,
        }


class TestCreateSessionAdvance:
    @pytest.mark.asyncio
    async def test_creates_advance_without_order_payment_side_effects(self, monkeypatch):
        conn = FakeConn()
        _patch_request_context(monkeypatch, conn)

        result = await svc.create_session_advance(
            object(),
            conn.table_id,
            Decimal("50000"),
            "cash",
            conn.payment_method_id,
            notes="cover",
        )

        assert result["success"] is True
        assert result["data"]["advance"]["table_session_id"] == str(conn.session_id)
        assert result["data"]["advance"]["payment_method_id"] == str(conn.payment_method_id)
        assert result["data"]["advance_totals"]["active_total_cop"] == 50000.0
        queries = _all_queries(conn)
        assert "INSERT INTO table_session_advances" in queries
        assert "order_payments" not in queries
        assert "order_items" not in queries

    @pytest.mark.asyncio
    async def test_validates_payment_method_id_against_group(self, monkeypatch):
        conn = FakeConn()
        _patch_request_context(monkeypatch, conn)

        await svc.create_session_advance(
            object(),
            conn.table_id,
            Decimal("50000"),
            "cash",
            conn.payment_method_id,
        )

        payment_validation_queries = [
            q for q in conn.fetchrow_queries
            if "FROM payment_methods" in q and "group_id = $3" in q
        ]
        assert payment_validation_queries


class TestVoidSessionAdvance:
    @pytest.mark.asyncio
    async def test_voids_advance_without_order_payment_side_effects(self, monkeypatch):
        conn = FakeConn()
        _patch_request_context(monkeypatch, conn)

        async def fetchrow(query, *args):
            conn.fetchrow_queries.append(query)
            compact = " ".join(query.split())
            if "FROM tables" in compact:
                return {"id": conn.table_id}
            if "FROM table_sessions" in compact:
                return {"id": conn.session_id, "table_id": conn.table_id}
            if "SELECT * FROM table_session_advances" in compact:
                return conn.advance_row.copy()
            if "UPDATE table_session_advances" in compact and "status = 'voided'" in compact:
                row = conn.advance_row.copy()
                row["status"] = "voided"
                row["void_reason"] = "wrong amount"
                return row
            if "FROM tenant_accounts" in compact:
                return None
            return None

        conn.fetchrow = fetchrow
        result = await svc.void_session_advance(
            object(),
            conn.table_id,
            conn.advance_id,
            "wrong amount",
        )

        assert result["success"] is True
        assert result["data"]["advance"]["status"] == "voided"
        queries = _all_queries(conn)
        assert "UPDATE table_session_advances" in queries
        assert "order_payments" not in queries
        assert "order_items" not in queries

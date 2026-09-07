"""Stale/duplicate KDS status updates — warocol.com#2037."""
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.core.exceptions import ValidationError
from app.services import comandas_service
from app.services.comandas_service import _is_stale_status_request


@pytest.mark.parametrize(
    "current,requested,expected",
    [
        ("preparing", "preparing", True),
        ("ready", "ready", True),
        ("delivered", "delivered", True),
        ("cancelled", "cancelled", True),
        ("ready", "preparing", True),
        ("delivered", "ready", True),
        ("delivered", "preparing", True),
        ("pending", "preparing", False),
        ("preparing", "ready", False),
        ("ready", "delivered", False),
        ("delivered", "cancelled", False),
        ("cancelled", "ready", False),
    ],
)
def test_is_stale_status_request(current, requested, expected):
    assert _is_stale_status_request(current, requested) is expected


@pytest.mark.asyncio
async def test_update_comanda_status_noop_when_already_ready():
    """Duplicate ready PATCH returns success without UPDATE."""
    comanda_id = uuid4()
    tenant_id = uuid4()
    request = MagicMock()

    mock_conn = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value=True)
    mock_conn.fetchrow = AsyncMock(
        return_value={
            "id": comanda_id,
            "status": "ready",
            "source_type": "table",
            "is_bar": False,
        }
    )
    mock_conn.execute = AsyncMock()

    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_cm.__aexit__ = AsyncMock(return_value=None)

    session = MagicMock(tenant_id=tenant_id)

    with patch("app.services.comandas_service.require_valid_session", return_value=session), \
         patch("app.services.comandas_service.get_db_connection", return_value=mock_cm):
        result = await comandas_service.update_comanda_status(request, comanda_id, "ready")

    assert result["success"] is True
    assert result.get("noop") is True
    mock_conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_update_comanda_status_noop_when_mesa_already_delivered():
    """Mesa-close race: KDS asks ready but comanda is already delivered."""
    comanda_id = uuid4()
    tenant_id = uuid4()
    request = MagicMock()

    mock_conn = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value=True)
    mock_conn.fetchrow = AsyncMock(
        return_value={
            "id": comanda_id,
            "status": "delivered",
            "source_type": "table",
            "is_bar": False,
        }
    )
    mock_conn.execute = AsyncMock()

    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_cm.__aexit__ = AsyncMock(return_value=None)

    session = MagicMock(tenant_id=tenant_id)

    with patch("app.services.comandas_service.require_valid_session", return_value=session), \
         patch("app.services.comandas_service.get_db_connection", return_value=mock_cm):
        result = await comandas_service.update_comanda_status(request, comanda_id, "ready")

    assert result["success"] is True
    assert result.get("noop") is True
    mock_conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_update_comanda_status_still_rejects_illegal_backward():
    """True illegal transitions (e.g. delivered → cancelled) still 400."""
    comanda_id = uuid4()
    tenant_id = uuid4()
    request = MagicMock()

    mock_conn = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value=True)
    mock_conn.fetchrow = AsyncMock(
        return_value={
            "id": comanda_id,
            "status": "delivered",
            "source_type": "table",
            "is_bar": False,
        }
    )

    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_cm.__aexit__ = AsyncMock(return_value=None)

    session = MagicMock(tenant_id=tenant_id)

    with patch("app.services.comandas_service.require_valid_session", return_value=session), \
         patch("app.services.comandas_service.get_db_connection", return_value=mock_cm):
        with pytest.raises(ValidationError):
            await comandas_service.update_comanda_status(request, comanda_id, "cancelled")

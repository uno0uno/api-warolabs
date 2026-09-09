from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.models.auth import UpdateProfileRequest


def test_tables_layout_override_accepts_grid_list_canvas():
    assert UpdateProfileRequest(pos_tables_layout_override="canvas").pos_tables_layout_override == "canvas"
    assert UpdateProfileRequest(pos_tables_layout_override=" GRID ").pos_tables_layout_override == "grid"
    assert UpdateProfileRequest(pos_tables_layout_override="").pos_tables_layout_override is None
    assert UpdateProfileRequest().pos_tables_layout_override is None


def test_tables_layout_override_rejects_unknown():
    with pytest.raises(ValidationError):
        UpdateProfileRequest(pos_tables_layout_override="carousel")


@pytest.mark.asyncio
async def test_update_profile_persists_tables_layout():
    from contextlib import asynccontextmanager

    from app.services import auth_service

    @asynccontextmanager
    async def _ctx():
        yield conn

    user_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "id": user_id,
            "email": "t@warocol.com",
            "name": "T",
            "user_name": None,
            "description": None,
            "logo_avatar": None,
            "preferred_locale": None,
            "pos_catalog_layout_override": None,
            "pos_tables_layout_override": "canvas",
            "created_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        }
    )
    session = SimpleNamespace(user_id=user_id)

    with (
        patch.object(auth_service, "require_valid_session", return_value=session),
        patch.object(auth_service, "get_db_connection", side_effect=lambda: _ctx()),
    ):
        result = await auth_service.update_profile(
            object(),
            pos_tables_layout_override="canvas",
            fields_set={"pos_tables_layout_override"},
        )

    query = conn.fetchrow.await_args.args[0]
    assert "pos_tables_layout_override = $1" in query
    assert result.user.pos_tables_layout_override == "canvas"

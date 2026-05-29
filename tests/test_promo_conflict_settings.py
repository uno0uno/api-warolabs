"""Tenant promo conflict settings — warocol.com#1011."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services.operaciones_context_service import update_promo_conflict_config
from app.services.promotions_service import (
    DEFAULT_PROMO_CONFLICT_STRATEGY,
    DEFAULT_PROMO_TYPE_BLOCK_MAP,
    normalize_promo_type_block_map,
    validate_promo_type_block_map,
)


def test_default_type_block_map():
    assert DEFAULT_PROMO_TYPE_BLOCK_MAP == {
        "bogo": ["percent_off", "fixed_off"],
    }


def test_normalize_promo_type_block_map_falls_back_to_default():
    assert normalize_promo_type_block_map(None) == DEFAULT_PROMO_TYPE_BLOCK_MAP
    assert normalize_promo_type_block_map({}) == DEFAULT_PROMO_TYPE_BLOCK_MAP


def test_normalize_promo_type_block_map_filters_invalid_entries():
    raw = {
        "bogo": ["percent_off", "unknown"],
        "invalid": ["percent_off"],
    }
    assert normalize_promo_type_block_map(raw) == {"bogo": ["percent_off"]}


def test_normalize_promo_type_block_map_parses_json_string():
    raw = '{"bogo": ["percent_off", "fixed_off"]}'
    assert normalize_promo_type_block_map(raw) == DEFAULT_PROMO_TYPE_BLOCK_MAP


def test_validate_promo_type_block_map_rejects_unknown_winner():
    with pytest.raises(ValueError, match="Unknown promo_type"):
        validate_promo_type_block_map({"combo": ["percent_off"]})


def test_validate_promo_type_block_map_rejects_unknown_blocked_type():
    with pytest.raises(ValueError, match="Unknown blocked promo_type"):
        validate_promo_type_block_map({"bogo": ["combo"]})


@pytest.mark.asyncio
async def test_update_promo_conflict_config_persists(monkeypatch):
    tenant_id = uuid4()
    row = {
        "promo_conflict_strategy": DEFAULT_PROMO_CONFLICT_STRATEGY,
        "promo_type_block_map": {"bogo": ["percent_off", "fixed_off"]},
    }
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=row)

    @asynccontextmanager
    async def _ctx():
        yield conn

    monkeypatch.setattr(
        "app.services.operaciones_context_service.get_db_connection",
        _ctx,
    )

    result = await update_promo_conflict_config(
        tenant_id,
        DEFAULT_PROMO_CONFLICT_STRATEGY,
        dict(DEFAULT_PROMO_TYPE_BLOCK_MAP),
    )

    assert result["success"] is True
    assert result["data"]["promo_conflict_strategy"] == "priority"
    assert result["data"]["promo_type_block_map"] == DEFAULT_PROMO_TYPE_BLOCK_MAP
    sql = conn.fetchrow.call_args[0][0]
    assert "promo_conflict_strategy" in sql
    assert "promo_type_block_map" in sql


@pytest.mark.asyncio
async def test_update_promo_conflict_config_rejects_unknown_strategy():
    with pytest.raises(HTTPException) as excinfo:
        await update_promo_conflict_config(
            uuid4(),
            "stack_all",
            dict(DEFAULT_PROMO_TYPE_BLOCK_MAP),
        )
    assert excinfo.value.status_code == 422


@pytest.mark.asyncio
async def test_update_promo_conflict_config_rejects_invalid_map():
    with pytest.raises(HTTPException) as excinfo:
        await update_promo_conflict_config(
            uuid4(),
            DEFAULT_PROMO_CONFLICT_STRATEGY,
            {"bogo": ["combo"]},
        )
    assert excinfo.value.status_code == 422

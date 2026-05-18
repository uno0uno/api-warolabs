"""CRUD for tenant_shift_templates under Operaciones (warocol.com#682)."""
from datetime import time
from typing import Any, Dict, Optional
from uuid import UUID

import asyncpg
from fastapi import HTTPException, Request

from app.core.exceptions import AuthenticationError
from app.core.middleware import require_valid_session
from app.database import get_db_connection
from app.models.shift_template import ShiftTemplateCreate, ShiftTemplatePatch, _validate_shift_window

import logging

logger = logging.getLogger(__name__)


def _row_to_dict(row: asyncpg.Record) -> Dict[str, Any]:
    data = dict(row)
    data["id"] = str(data["id"])
    data["tenant_id"] = str(data["tenant_id"])
    for key in ("start_time", "end_time"):
        if data[key] is not None:
            data[key] = data[key].isoformat()
    for key in ("created_at", "updated_at"):
        if data[key] is not None:
            data[key] = data[key].isoformat()
    return data


async def list_shift_templates(
    request: Request,
    *,
    include_inactive: bool = False,
) -> dict:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    query = """
        SELECT *
        FROM tenant_shift_templates
        WHERE tenant_id = $1
    """
    if not include_inactive:
        query += " AND is_active = true"
    query += " ORDER BY sort_order, name"

    async with get_db_connection(use_transaction=False) as conn:
        rows = await conn.fetch(query, tenant_id)

    return {"success": True, "data": [_row_to_dict(r) for r in rows]}


async def create_shift_template(request: Request, body: ShiftTemplateCreate) -> dict:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    try:
        async with get_db_connection() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO tenant_shift_templates (
                    tenant_id, name, start_time, end_time,
                    crosses_midnight, sort_order
                )
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING *
                """,
                tenant_id,
                body.name.strip(),
                body.start_time,
                body.end_time,
                body.crosses_midnight,
                body.sort_order,
            )
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=409,
            detail="Ya existe un turno con ese nombre",
        ) from None

    logger.info("Created shift template %r for tenant %s", body.name, tenant_id)
    return {"success": True, "data": _row_to_dict(row)}


async def patch_shift_template(
    request: Request,
    template_id: UUID,
    body: ShiftTemplatePatch,
) -> dict:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    data_dict = body.model_dump(exclude_unset=True)
    if not data_dict:
        raise HTTPException(status_code=400, detail="No fields to update")

    if "name" in data_dict and data_dict["name"] is not None:
        data_dict["name"] = data_dict["name"].strip()

    async with get_db_connection() as conn:
        existing = await conn.fetchrow(
            """
            SELECT start_time, end_time, crosses_midnight
            FROM tenant_shift_templates
            WHERE id = $1 AND tenant_id = $2
            """,
            template_id,
            tenant_id,
        )
        if not existing:
            raise HTTPException(status_code=404, detail="Shift template not found")

        merged_start: time = data_dict.get("start_time", existing["start_time"])
        merged_end: time = data_dict.get("end_time", existing["end_time"])
        merged_crosses: bool = data_dict.get(
            "crosses_midnight", existing["crosses_midnight"]
        )
        try:
            _validate_shift_window(
                start_time=merged_start,
                end_time=merged_end,
                crosses_midnight=merged_crosses,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        update_fields = []
        params = []
        param_counter = 1

        for field, value in data_dict.items():
            update_fields.append(f"{field} = ${param_counter}")
            params.append(value)
            param_counter += 1

        update_fields.append("updated_at = now()")
        params.extend([tenant_id, template_id])

        try:
            row = await conn.fetchrow(
                f"""
                UPDATE tenant_shift_templates
                SET {', '.join(update_fields)}
                WHERE tenant_id = ${param_counter} AND id = ${param_counter + 1}
                RETURNING *
                """,
                *params,
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(
                status_code=409,
                detail="Ya existe un turno con ese nombre",
            ) from None

    if not row:
        raise HTTPException(status_code=404, detail="Shift template not found")

    return {"success": True, "data": _row_to_dict(row)}

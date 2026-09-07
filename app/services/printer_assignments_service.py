"""Tenant printer assignments for POS routing (warocol.com#1949 / epic #1947).

Stores QZ/OS printer names for:
- caja (prefactura + factura)
- optional kitchen station printers (fallback → caja)
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from app.core.exceptions import AuthenticationError
from app.core.middleware import require_valid_session
from app.database import get_db_connection


class StationPrinterAssignment(BaseModel):
    station_id: UUID
    printer_name: Optional[str] = None

    @field_validator("printer_name")
    @classmethod
    def _strip_name(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class PrinterAssignmentsPut(BaseModel):
    caja_printer_name: Optional[str] = None
    stations: List[StationPrinterAssignment] = Field(default_factory=list)

    @field_validator("caja_printer_name")
    @classmethod
    def _strip_caja(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


def resolve_printer_name(
    *,
    caja_printer_name: Optional[str],
    station_map: Dict[str, str],
    station_id: Optional[str] = None,
) -> Optional[str]:
    """Product rule: station mapping if set, else caja. Missing caja → None."""
    if station_id:
        mapped = station_map.get(str(station_id))
        if mapped:
            return mapped
    return caja_printer_name or None


def _rows_to_payload(rows: list) -> Dict[str, Any]:
    caja_printer_name: Optional[str] = None
    stations: List[Dict[str, str]] = []
    station_map: Dict[str, str] = {}
    for row in rows:
        role = row["role"]
        name = (row["printer_name"] or "").strip()
        if not name:
            continue
        if role == "caja":
            caja_printer_name = name
        elif role == "station" and row["station_id"] is not None:
            sid = str(row["station_id"])
            stations.append({"station_id": sid, "printer_name": name})
            station_map[sid] = name
    return {
        "caja_printer_name": caja_printer_name,
        "stations": stations,
        "station_map": station_map,
    }


async def get_printer_assignments(request: Request) -> dict:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    async with get_db_connection(use_transaction=False) as conn:
        rows = await conn.fetch(
            """
            SELECT role, station_id, printer_name
            FROM tenant_printer_assignments
            WHERE tenant_id = $1
            ORDER BY role, station_id
            """,
            tenant_id,
        )
        active_stations = await conn.fetch(
            """
            SELECT id, name, kitchen_name, is_active
            FROM kitchen_stations
            WHERE tenant_id = $1 AND is_active = true
            ORDER BY display_order, name
            """,
            tenant_id,
        )

    payload = _rows_to_payload(rows)
    resolved = {
        str(st["id"]): resolve_printer_name(
            caja_printer_name=payload["caja_printer_name"],
            station_map=payload["station_map"],
            station_id=str(st["id"]),
        )
        for st in active_stations
    }

    return {
        "success": True,
        "data": {
            "caja_printer_name": payload["caja_printer_name"],
            "stations": payload["stations"],
            "active_stations": [
                {
                    "id": str(st["id"]),
                    "name": st["name"],
                    "kitchen_name": st["kitchen_name"],
                }
                for st in active_stations
            ],
            "resolved": resolved,
            "resolved_caja": payload["caja_printer_name"],
        },
    }


async def put_printer_assignments(request: Request, body: PrinterAssignmentsPut) -> dict:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    station_writes: Dict[UUID, str] = {}
    for item in body.stations:
        if item.printer_name:
            station_writes[item.station_id] = item.printer_name

    async with get_db_connection() as conn:
        if station_writes:
            found = await conn.fetchval(
                """
                SELECT COUNT(*)::int
                FROM kitchen_stations
                WHERE tenant_id = $1 AND id = ANY($2::uuid[])
                """,
                tenant_id,
                list(station_writes.keys()),
            )
            if found != len(station_writes):
                raise HTTPException(
                    status_code=400,
                    detail="One or more station_id values are invalid for this tenant",
                )

        await conn.execute(
            "DELETE FROM tenant_printer_assignments WHERE tenant_id = $1",
            tenant_id,
        )

        if body.caja_printer_name:
            await conn.execute(
                """
                INSERT INTO tenant_printer_assignments (
                    tenant_id, role, station_id, printer_name
                )
                VALUES ($1, 'caja', NULL, $2)
                """,
                tenant_id,
                body.caja_printer_name,
            )

        for station_id, printer_name in station_writes.items():
            await conn.execute(
                """
                INSERT INTO tenant_printer_assignments (
                    tenant_id, role, station_id, printer_name
                )
                VALUES ($1, 'station', $2, $3)
                """,
                tenant_id,
                station_id,
                printer_name,
            )

    return await get_printer_assignments(request)

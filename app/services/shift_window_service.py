"""Resolve shift template + calendar date to cierre period fields (warocol.com#684)."""
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, Optional
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request

from app.core.exceptions import AuthenticationError
from app.core.middleware import require_valid_session
from app.database import get_db_connection

BOGOTA = ZoneInfo("America/Bogota")


def resolve_shift_template_window(
    *,
    anchor_date: date,
    start_time: time,
    end_time: time,
    crosses_midnight: bool,
    template_id: Optional[UUID] = None,
    template_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Pure resolver: template clock times + anchor date → cierre period fields (Bogotá)."""
    period_start = anchor_date
    period_end = anchor_date + timedelta(days=1) if crosses_midnight else anchor_date

    period_start_time = datetime.combine(anchor_date, start_time, tzinfo=BOGOTA)
    period_end_time = datetime.combine(period_end, end_time, tzinfo=BOGOTA)

    payload: Dict[str, Any] = {
        "periodStart": period_start.isoformat(),
        "periodEnd": period_end.isoformat(),
        "periodStartTime": period_start_time.isoformat(),
        "periodEndTime": period_end_time.isoformat(),
        "crossesMidnight": crosses_midnight,
    }
    if template_id is not None:
        payload["templateId"] = str(template_id)
    if template_name is not None:
        payload["templateName"] = template_name
    return payload


async def get_template_window(
    request: Request,
    template_id: UUID,
    anchor_date: date,
) -> dict:
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    async with get_db_connection(use_transaction=False) as conn:
        row = await conn.fetchrow(
            """
            SELECT id, name, start_time, end_time, crosses_midnight
            FROM tenant_shift_templates
            WHERE id = $1 AND tenant_id = $2 AND is_active = true
            """,
            template_id,
            tenant_id,
        )

    if not row:
        raise HTTPException(status_code=404, detail="Shift template not found")

    data = resolve_shift_template_window(
        anchor_date=anchor_date,
        start_time=row["start_time"],
        end_time=row["end_time"],
        crosses_midnight=row["crosses_midnight"],
        template_id=row["id"],
        template_name=row["name"],
    )
    return {"success": True, "data": data}


async def get_suggested_window(
    request: Request,
    anchor_date: date,
) -> dict:
    """Suggest a custom arqueo window from last close end through now (Bogotá)."""
    session = require_valid_session(request)
    tenant_id = session.tenant_id
    if not tenant_id:
        raise AuthenticationError("Tenant ID is required")

    now_bogota = datetime.now(BOGOTA)

    async with get_db_connection(use_transaction=False) as conn:
        row = await conn.fetchrow(
            """
            SELECT
                ap.period_start,
                ap.period_end,
                ap.period_start_time,
                ap.period_end_time
            FROM accounting_period ap
            WHERE ap.tenant_id = $1
              AND ap.deleted_at IS NULL
            ORDER BY
                COALESCE(
                    ap.period_end_time,
                    (ap.period_end::timestamp + INTERVAL '23:59:59')
                        AT TIME ZONE 'America/Bogota'
                ) DESC
            LIMIT 1
            """,
            tenant_id,
        )

    if row and row["period_end_time"]:
        period_start_time = row["period_end_time"]
        period_start = period_start_time.astimezone(BOGOTA).date()
    elif row:
        period_start = row["period_end"]
        period_start_time = datetime.combine(period_start, time(0, 0, 0), tzinfo=BOGOTA)
    else:
        period_start = anchor_date
        period_start_time = datetime.combine(anchor_date, time(0, 0, 0), tzinfo=BOGOTA)

    period_end = now_bogota.date()
    period_end_time = now_bogota

    if period_start_time >= period_end_time:
        raise HTTPException(
            status_code=422,
            detail="No hay ventana sugerida: el último cierre es posterior a ahora",
        )

    return {
        "success": True,
        "data": {
            "periodStart": period_start.isoformat(),
            "periodEnd": period_end.isoformat(),
            "periodStartTime": period_start_time.isoformat(),
            "periodEndTime": period_end_time.isoformat(),
            "suggested": True,
        },
    }

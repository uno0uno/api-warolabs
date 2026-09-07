"""Waiter assignment service (warocol.com#573, #574).

Manages the default waiter assigned to each table AND the per-session
override:
- `assign_member_to_table` (#573): atomic transaction that closes the
  previous period in `table_member_assignments`, opens a new one with
  snapshots, and updates the `tables.assigned_member_id` pointer.
- `get_assignment_history` (#573): paginated history for a single table.
- `set_session_waiter` (#574): set/clear the per-session override on the
  currently-open session for a table. Enforces auto-handoff: only the
  current waiter or supervisor+ can reassign.

All gated by `assert_waiter_attribution_enabled` to reject writes/reads
when the feature flag is off for the tenant.

All public functions are async; use `Optional[X]` and `List[X]` for
Python 3.9 target compatibility.
"""
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import HTTPException

from app.core.permissions import can_reassign_waiter
from app.database import get_db_connection
from app.services.operaciones_context_service import assert_waiter_attribution_enabled
from app.services.table_session_guests import guest_snapshot_from_capacity, normalize_custom_label


async def assign_member_to_table(
    tenant_id: UUID,
    table_id: UUID,
    member_id: Optional[UUID],
    assigned_by_user_id: Optional[UUID],
) -> Dict[str, Any]:
    """Set or clear the default waiter for a table.

    Atomic transaction:
      1. Close the currently open assignment period for the table.
      2. Insert a new period row with snapshots of member name/role
         (so the history row survives member deletion).
      3. Update `tables.assigned_member_id` pointer for fast reads.

    Validations:
      - `waiter_attribution_enabled = true` for the tenant (else 409).
      - Table belongs to tenant, is active, not deleted (else 404).
      - Table is NOT a bar (else 400).
      - If `member_id` is set, the member belongs to the tenant and is
        active (else 404).
    """
    # 1. Toggle must be ON.
    await assert_waiter_attribution_enabled(tenant_id)

    async with get_db_connection() as conn:
        async with conn.transaction():
            # 2. Validate the table.
            table_row = await conn.fetchrow(
                """
                SELECT id, is_bar, is_active, deleted_at
                FROM tables
                WHERE id = $1 AND tenant_id = $2
                """,
                table_id,
                tenant_id,
            )
            if table_row is None or table_row["deleted_at"] is not None or not table_row["is_active"]:
                raise HTTPException(status_code=404, detail="Table not found")
            if table_row["is_bar"]:
                raise HTTPException(
                    status_code=400,
                    detail="Bar tables cannot have an assigned waiter",
                )

            # 3. Validate the incoming member (when not clearing).
            member_name: Optional[str] = None
            member_role: Optional[str] = None
            if member_id is not None:
                member_row = await conn.fetchrow(
                    """
                    SELECT tm.id, tm.role, p.name
                    FROM tenant_members tm
                    JOIN profile p ON p.id = tm.user_id
                    WHERE tm.id = $1
                      AND tm.tenant_id = $2
                      AND tm.is_active = true
                      AND tm.terminated_at IS NULL
                    """,
                    member_id,
                    tenant_id,
                )
                if member_row is None:
                    raise HTTPException(status_code=404, detail="Member not found")
                member_name = member_row["name"] or "Sin nombre"
                member_role = member_row["role"]

            # 4. Close the previous open period (if any).
            await conn.execute(
                """
                UPDATE table_member_assignments
                SET unassigned_at = now()
                WHERE table_id = $1 AND unassigned_at IS NULL
                """,
                table_id,
            )

            # 5. Insert the new period (even if member_id is NULL — records
            #    the "unassigned" transition for audit completeness).
            if member_id is not None:
                await conn.execute(
                    """
                    INSERT INTO table_member_assignments
                        (tenant_id, table_id, member_id, member_name, member_role, assigned_by)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    tenant_id,
                    table_id,
                    member_id,
                    member_name,
                    member_role,
                    assigned_by_user_id,
                )

            # 6. Update the fast-lookup pointer on the tables row.
            await conn.execute(
                """
                UPDATE tables
                SET assigned_member_id = $1
                WHERE id = $2
                """,
                member_id,
                table_id,
            )

    return {
        "success": True,
        "data": {
            "table_id": str(table_id),
            "assigned_member_id": str(member_id) if member_id else None,
            "assigned_member_name": member_name,
            "assigned_member_role": member_role,
        },
    }


async def get_assignment_history(
    tenant_id: UUID,
    table_id: UUID,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    """Return paginated history of waiter assignments for a single table.

    Most recent first. Snapshots (member_name, member_role) come directly
    from the history row — not from a JOIN with `tenant_members` — so the
    response is correct even if the member was deleted afterwards.

    Validations:
      - `waiter_attribution_enabled = true` for the tenant (else 409).
      - Table belongs to tenant (else 404).
    """
    await assert_waiter_attribution_enabled(tenant_id)

    async with get_db_connection(use_transaction=False) as conn:
        table_check = await conn.fetchval(
            "SELECT 1 FROM tables WHERE id = $1 AND tenant_id = $2",
            table_id,
            tenant_id,
        )
        if table_check is None:
            raise HTTPException(status_code=404, detail="Table not found")

        rows = await conn.fetch(
            """
            SELECT
                tma.id,
                tma.member_id,
                tma.member_name,
                tma.member_role,
                tma.assigned_at,
                tma.unassigned_at,
                tma.assigned_by,
                p.name AS assigned_by_name
            FROM table_member_assignments tma
            LEFT JOIN profile p ON p.id = tma.assigned_by
            WHERE tma.table_id = $1 AND tma.tenant_id = $2
            ORDER BY tma.assigned_at DESC
            LIMIT $3 OFFSET $4
            """,
            table_id,
            tenant_id,
            limit,
            offset,
        )

    entries: List[Dict[str, Any]] = [
        {
            "id": str(r["id"]),
            "member_id": str(r["member_id"]) if r["member_id"] else None,
            "member_name": r["member_name"],
            "member_role": r["member_role"],
            "assigned_at": r["assigned_at"].isoformat() if r["assigned_at"] else None,
            "unassigned_at": r["unassigned_at"].isoformat() if r["unassigned_at"] else None,
            "assigned_by": str(r["assigned_by"]) if r["assigned_by"] else None,
            "assigned_by_name": r["assigned_by_name"],
        }
        for r in rows
    ]

    return {
        "success": True,
        "data": entries,
        "limit": limit,
        "offset": offset,
    }


async def set_session_waiter(
    tenant_id: UUID,
    table_id: UUID,
    member_id: Optional[UUID],
    caller_user_id: Optional[UUID],
    caller_role: Optional[str],
) -> Dict[str, Any]:
    """Set or clear the per-session waiter override (warocol.com#574).

    The override lives on `table_sessions.attended_by_member_id`. When
    NULL, the resolver falls back to `tables.assigned_member_id` (#573).

    Auto-handoff guard (via `can_reassign_waiter`):
      - No current waiter on the session → anyone with POS access can set.
      - Caller IS the current waiter → can hand off to another or clear.
      - Caller is supervisor+ → can override regardless.
      - Else → 403 Forbidden.

    Validations:
      - `waiter_attribution_enabled = true` (else 409)
      - Open session exists for the table (else 404)
      - `member_id`, if set, belongs to the tenant and is active (else 404)
    """
    await assert_waiter_attribution_enabled(tenant_id)

    async with get_db_connection() as conn:
        async with conn.transaction():
            # Lock the open session row to prevent racing PATCHes.
            session_row = await conn.fetchrow(
                """
                SELECT id, attended_by_member_id
                FROM table_sessions
                WHERE table_id = $1 AND tenant_id = $2 AND closed_at IS NULL
                FOR UPDATE
                """,
                table_id,
                tenant_id,
            )
            if session_row is None:
                raise HTTPException(status_code=404, detail="No open session for this table")

            # Auto-handoff check
            allowed = await can_reassign_waiter(
                caller_user_id=caller_user_id,
                caller_role=caller_role,
                tenant_id=tenant_id,
                current_waiter_member_id=session_row["attended_by_member_id"],
            )
            if not allowed:
                raise HTTPException(
                    status_code=403,
                    detail="Only the current waiter or a supervisor can reassign this session",
                )

            # Validate incoming member if not clearing
            member_name: Optional[str] = None
            member_role: Optional[str] = None
            if member_id is not None:
                member_row = await conn.fetchrow(
                    """
                    SELECT tm.id, tm.role, p.name
                    FROM tenant_members tm
                    JOIN profile p ON p.id = tm.user_id
                    WHERE tm.id = $1
                      AND tm.tenant_id = $2
                      AND tm.is_active = true
                      AND tm.terminated_at IS NULL
                    """,
                    member_id,
                    tenant_id,
                )
                if member_row is None:
                    raise HTTPException(status_code=404, detail="Member not found")
                member_name = member_row["name"] or "Sin nombre"
                member_role = member_row["role"]

            # Apply the change
            await conn.execute(
                "UPDATE table_sessions SET attended_by_member_id = $1 WHERE id = $2",
                member_id,
                session_row["id"],
            )

    return {
        "success": True,
        "data": {
            "session_id": str(session_row["id"]),
            "attended_by_member_id": str(member_id) if member_id else None,
            "attended_by_member_name": member_name,
            "attended_by_member_role": member_role,
        },
    }


async def set_session_guests(
    tenant_id: UUID,
    table_id: UUID,
    covers: Optional[int],
    custom_label: Optional[str],
    custom_label_provided: bool,
) -> Dict[str, Any]:
    """Update covers and/or custom_label on the open table session (#2469)."""
    if covers is None and not custom_label_provided:
        raise HTTPException(status_code=400, detail="covers or custom_label is required")

    label_value = normalize_custom_label(custom_label) if custom_label_provided else None

    async with get_db_connection() as conn:
        async with conn.transaction():
            session_row = await conn.fetchrow(
                """
                SELECT id, covers, custom_label, capacity_snapshot
                FROM table_sessions
                WHERE table_id = $1 AND tenant_id = $2 AND closed_at IS NULL
                FOR UPDATE
                """,
                table_id,
                tenant_id,
            )
            if session_row is None:
                raise HTTPException(status_code=404, detail="No open session for this table")

            new_covers = covers if covers is not None else session_row["covers"]
            new_label = label_value if custom_label_provided else session_row["custom_label"]
            new_capacity_snapshot = session_row["capacity_snapshot"]
            if new_capacity_snapshot is None:
                table_capacity = await conn.fetchval(
                    "SELECT capacity FROM tables WHERE id = $1 AND tenant_id = $2",
                    table_id,
                    tenant_id,
                )
                _, new_capacity_snapshot = guest_snapshot_from_capacity(table_capacity)

            await conn.execute(
                """
                UPDATE table_sessions
                SET covers = $1, custom_label = $2, capacity_snapshot = $3
                WHERE id = $4
                """,
                new_covers,
                new_label,
                new_capacity_snapshot,
                session_row["id"],
            )

    return {
        "success": True,
        "data": {
            "session_id": str(session_row["id"]),
            "covers": new_covers,
            "custom_label": new_label,
            "capacity_snapshot": new_capacity_snapshot,
        },
    }


async def set_order_served_by(
    tenant_id: UUID,
    order_id: UUID,
    member_id: Optional[UUID],
    caller_user_id: Optional[UUID],
    caller_role: Optional[str],
) -> Dict[str, Any]:
    """Set or clear the per-order waiter (warocol.com#575).

    Auto-handoff guard (reuses `can_reassign_waiter` from #574):
      - No current `served_by` on the order → anyone with POS can set.
      - Caller IS the current `served_by` → can hand off / clear.
      - Caller is supervisor+ → can override.
      - Else → 403 Forbidden.

    Validations:
      - `waiter_attribution_enabled = true` for the tenant (else 409)
      - Order exists + belongs to tenant (else 404)
      - `member_id`, if set, belongs to tenant + active (else 404)
    """
    await assert_waiter_attribution_enabled(tenant_id)

    async with get_db_connection() as conn:
        async with conn.transaction():
            order_row = await conn.fetchrow(
                """
                SELECT id, served_by_member_id
                FROM orders
                WHERE id = $1 AND tenant_id = $2
                FOR UPDATE
                """,
                order_id,
                tenant_id,
            )
            if order_row is None:
                raise HTTPException(status_code=404, detail="Order not found")

            allowed = await can_reassign_waiter(
                caller_user_id=caller_user_id,
                caller_role=caller_role,
                tenant_id=tenant_id,
                current_waiter_member_id=order_row["served_by_member_id"],
            )
            if not allowed:
                raise HTTPException(
                    status_code=403,
                    detail="Only the current server or a supervisor can reassign this order",
                )

            member_name: Optional[str] = None
            member_role: Optional[str] = None
            if member_id is not None:
                member_row = await conn.fetchrow(
                    """
                    SELECT tm.id, tm.role, p.name
                    FROM tenant_members tm
                    JOIN profile p ON p.id = tm.user_id
                    WHERE tm.id = $1
                      AND tm.tenant_id = $2
                      AND tm.is_active = true
                      AND tm.terminated_at IS NULL
                    """,
                    member_id,
                    tenant_id,
                )
                if member_row is None:
                    raise HTTPException(status_code=404, detail="Member not found")
                member_name = member_row["name"] or "Sin nombre"
                member_role = member_row["role"]

            await conn.execute(
                "UPDATE orders SET served_by_member_id = $1, updated_at = now() WHERE id = $2",
                member_id,
                order_id,
            )

    return {
        "success": True,
        "data": {
            "order_id": str(order_id),
            "served_by_member_id": str(member_id) if member_id else None,
            "served_by_member_name": member_name,
            "served_by_member_role": member_role,
        },
    }

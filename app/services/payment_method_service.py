"""
Payment Method Service — CRUD for groups and methods, plus POS read.
Issue: https://github.com/uno0uno/warocol.com/issues/331
"""
import logging
from uuid import UUID
from fastapi import Request
from asyncpg import UniqueViolationError
from app.database import get_db_connection
from app.core.middleware import require_valid_session
from app.core.exceptions import NotFoundError, AuthorizationError, ValidationError
from app.models.payment_method import (
    CreateGroupRequest,
    PatchGroupRequest,
    CreateMethodRequest,
    PatchMethodRequest,
)

logger = logging.getLogger(__name__)


# ── Groups ─────────────────────────────────────────────────────────────────────

async def list_groups(request: Request) -> dict:
    """
    Returns all groups visible to the tenant:
      - global defaults (tenant_id IS NULL)
      - tenant's own custom groups
    Includes method_count: number of active methods the tenant has per group.
    """
    session = require_valid_session(request)
    tenant_id = session.tenant_id

    async with get_db_connection(use_transaction=False) as conn:
        rows = await conn.fetch(
            """
            SELECT
                pmg.id,
                pmg.tenant_id,
                pmg.name,
                pmg.slug,
                pmg.triggers_cartera,
                pmg.is_active,
                pmg.sort_order,
                pmg.gl_account_code,
                COUNT(pm.id) FILTER (
                    WHERE pm.is_active = true AND pm.tenant_id = $1
                ) AS method_count
            FROM payment_method_groups pmg
            LEFT JOIN payment_methods pm ON pm.group_id = pmg.id
            WHERE pmg.tenant_id IS NULL OR pmg.tenant_id = $1
            GROUP BY pmg.id
            ORDER BY pmg.sort_order, pmg.name
            """,
            tenant_id,
        )

    data = [
        {
            "id": str(row["id"]),
            "tenantId": str(row["tenant_id"]) if row["tenant_id"] else None,
            "name": row["name"],
            "slug": row["slug"],
            "triggersCartera": row["triggers_cartera"],
            "isActive": row["is_active"],
            "sortOrder": row["sort_order"],
            "glAccountCode": row["gl_account_code"],
            "methodCount": row["method_count"],
        }
        for row in rows
    ]
    return {"success": True, "data": data}


async def create_group(request: Request, body: CreateGroupRequest) -> dict:
    """Create a custom payment method group owned by the tenant."""
    session = require_valid_session(request)
    tenant_id = session.tenant_id

    async with get_db_connection(use_transaction=True) as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO payment_method_groups
                    (tenant_id, name, slug, triggers_cartera, sort_order)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id, tenant_id, name, slug, triggers_cartera, is_active, sort_order
                """,
                tenant_id,
                body.name,
                body.slug,
                body.triggersCartera,
                body.sortOrder,
            )
        except UniqueViolationError:
            raise ValidationError(f"A group with slug '{body.slug}' already exists for this tenant.")

    return {
        "success": True,
        "data": {
            "id": str(row["id"]),
            "tenantId": str(row["tenant_id"]),
            "name": row["name"],
            "slug": row["slug"],
            "triggersCartera": row["triggers_cartera"],
            "isActive": row["is_active"],
            "sortOrder": row["sort_order"],
            "methodCount": 0,
        },
    }


async def patch_group(request: Request, group_id: UUID, body: PatchGroupRequest) -> dict:
    """
    Update a group's name, sort_order, is_active, or triggers_cartera.
    Returns 403 if the group is a global default (tenant_id IS NULL).
    """
    session = require_valid_session(request)
    tenant_id = session.tenant_id

    async with get_db_connection(use_transaction=True) as conn:
        existing = await conn.fetchrow(
            "SELECT id, tenant_id FROM payment_method_groups WHERE id = $1",
            group_id,
        )
        if existing is None:
            raise NotFoundError("Payment method group not found.")
        if existing["tenant_id"] is None:
            raise AuthorizationError("Cannot modify a global default payment method group.")
        if str(existing["tenant_id"]) != str(tenant_id):
            raise NotFoundError("Payment method group not found.")

        # Build SET clause dynamically — only update provided fields
        updates = []
        params: list = []
        idx = 1

        if body.name is not None:
            updates.append(f"name = ${idx}")
            params.append(body.name)
            idx += 1
        if body.isActive is not None:
            updates.append(f"is_active = ${idx}")
            params.append(body.isActive)
            idx += 1
        if body.sortOrder is not None:
            updates.append(f"sort_order = ${idx}")
            params.append(body.sortOrder)
            idx += 1
        if body.triggersCartera is not None:
            updates.append(f"triggers_cartera = ${idx}")
            params.append(body.triggersCartera)
            idx += 1
        if body.glAccountCode is not None:
            updates.append(f"gl_account_code = ${idx}")
            params.append(body.glAccountCode if body.glAccountCode != "" else None)
            idx += 1

        if not updates:
            # Nothing to update — return current state
            row = await conn.fetchrow(
                """
                SELECT id, tenant_id, name, slug, triggers_cartera, is_active, sort_order, gl_account_code
                FROM payment_method_groups WHERE id = $1
                """,
                group_id,
            )
        else:
            params.append(group_id)
            row = await conn.fetchrow(
                f"""
                UPDATE payment_method_groups
                SET {', '.join(updates)}
                WHERE id = ${idx}
                RETURNING id, tenant_id, name, slug, triggers_cartera, is_active, sort_order, gl_account_code
                """,
                *params,
            )

    return {
        "success": True,
        "data": {
            "id": str(row["id"]),
            "tenantId": str(row["tenant_id"]) if row["tenant_id"] else None,
            "name": row["name"],
            "slug": row["slug"],
            "triggersCartera": row["triggers_cartera"],
            "isActive": row["is_active"],
            "sortOrder": row["sort_order"],
            "glAccountCode": row["gl_account_code"],
        },
    }


# ── Methods ────────────────────────────────────────────────────────────────────

async def list_methods(request: Request) -> dict:
    """List all methods belonging to the tenant (all is_active states)."""
    session = require_valid_session(request)
    tenant_id = session.tenant_id

    async with get_db_connection(use_transaction=False) as conn:
        rows = await conn.fetch(
            """
            SELECT pm.id, pm.tenant_id, pm.group_id, pm.name, pm.is_active, pm.sort_order, pm.gl_account_code
            FROM payment_methods pm
            WHERE pm.tenant_id = $1
            ORDER BY pm.sort_order, pm.name
            """,
            tenant_id,
        )

    data = [
        {
            "id": str(row["id"]),
            "tenantId": str(row["tenant_id"]),
            "groupId": str(row["group_id"]),
            "name": row["name"],
            "isActive": row["is_active"],
            "sortOrder": row["sort_order"],
            "glAccountCode": row["gl_account_code"],
        }
        for row in rows
    ]
    return {"success": True, "data": data}


async def create_method(request: Request, body: CreateMethodRequest) -> dict:
    """Create a method (subtype) under a group."""
    session = require_valid_session(request)
    tenant_id = session.tenant_id

    group_id = UUID(body.groupId)

    async with get_db_connection(use_transaction=True) as conn:
        # Verify group is visible to tenant
        group = await conn.fetchrow(
            """
            SELECT id, slug FROM payment_method_groups
            WHERE id = $1 AND (tenant_id IS NULL OR tenant_id = $2)
            """,
            group_id,
            tenant_id,
        )
        if group is None:
            raise NotFoundError("Payment method group not found.")

        if group["slug"] == "cash":
            raise ValidationError(
                "Cannot add methods to the Efectivo group. "
                "Cash is a single built-in payment method."
            )

        try:
            # Issue #533 — accept gl_account_code on creation so the auto-create
            # flow in /finanzas/metodos-pago/{groupId} can link the new method
            # to its sub-account in a single round-trip (instead of POST + PATCH).
            row = await conn.fetchrow(
                """
                INSERT INTO payment_methods (tenant_id, group_id, name, sort_order, gl_account_code)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id, tenant_id, group_id, name, is_active, sort_order, gl_account_code
                """,
                tenant_id,
                group_id,
                body.name,
                body.sortOrder,
                body.glAccountCode if body.glAccountCode else None,
            )
        except UniqueViolationError:
            raise ValidationError(
                f"A method named '{body.name}' already exists in this group."
            )

    return {
        "success": True,
        "data": {
            "id": str(row["id"]),
            "tenantId": str(row["tenant_id"]),
            "groupId": str(row["group_id"]),
            "name": row["name"],
            "isActive": row["is_active"],
            "sortOrder": row["sort_order"],
            "glAccountCode": row["gl_account_code"],
        },
    }


async def patch_method(request: Request, method_id: UUID, body: PatchMethodRequest) -> dict:
    """Update a method's name, groupId, is_active, or sort_order."""
    session = require_valid_session(request)
    tenant_id = session.tenant_id

    async with get_db_connection(use_transaction=True) as conn:
        existing = await conn.fetchrow(
            "SELECT id, tenant_id FROM payment_methods WHERE id = $1 AND tenant_id = $2",
            method_id,
            tenant_id,
        )
        if existing is None:
            raise NotFoundError("Payment method not found.")

        updates = []
        params: list = []
        idx = 1

        if body.name is not None:
            updates.append(f"name = ${idx}")
            params.append(body.name)
            idx += 1
        if body.isActive is not None:
            updates.append(f"is_active = ${idx}")
            params.append(body.isActive)
            idx += 1
        if body.sortOrder is not None:
            updates.append(f"sort_order = ${idx}")
            params.append(body.sortOrder)
            idx += 1
        if body.groupId is not None:
            # Verify target group is visible to tenant
            new_group_id = UUID(body.groupId)
            group = await conn.fetchrow(
                """
                SELECT id FROM payment_method_groups
                WHERE id = $1 AND (tenant_id IS NULL OR tenant_id = $2)
                """,
                new_group_id,
                tenant_id,
            )
            if group is None:
                raise NotFoundError("Target payment method group not found.")
            updates.append(f"group_id = ${idx}")
            params.append(new_group_id)
            idx += 1
        if body.glAccountCode is not None:
            updates.append(f"gl_account_code = ${idx}")
            params.append(body.glAccountCode if body.glAccountCode != "" else None)
            idx += 1

        if not updates:
            row = await conn.fetchrow(
                "SELECT id, tenant_id, group_id, name, is_active, sort_order, gl_account_code FROM payment_methods WHERE id = $1",
                method_id,
            )
        else:
            params.append(method_id)
            row = await conn.fetchrow(
                f"""
                UPDATE payment_methods
                SET {', '.join(updates)}
                WHERE id = ${idx}
                RETURNING id, tenant_id, group_id, name, is_active, sort_order, gl_account_code
                """,
                *params,
            )

    return {
        "success": True,
        "data": {
            "id": str(row["id"]),
            "tenantId": str(row["tenant_id"]),
            "groupId": str(row["group_id"]),
            "name": row["name"],
            "isActive": row["is_active"],
            "sortOrder": row["sort_order"],
            "glAccountCode": row["gl_account_code"],
        },
    }


async def delete_method(request: Request, method_id: UUID) -> dict:
    """Soft delete a method (sets is_active = false)."""
    session = require_valid_session(request)
    tenant_id = session.tenant_id

    async with get_db_connection(use_transaction=True) as conn:
        row = await conn.fetchrow(
            """
            UPDATE payment_methods
            SET is_active = false
            WHERE id = $1 AND tenant_id = $2
            RETURNING id
            """,
            method_id,
            tenant_id,
        )
        if row is None:
            raise NotFoundError("Payment method not found.")

    return {"success": True, "data": {"id": str(row["id"])}}


# ── POS read-only ──────────────────────────────────────────────────────────────

async def _list_payment_methods_by_tenant_id(
    tenant_id,
    exclude_cartera: bool = False,
) -> dict:
    """
    Internal helper — returns active groups + nested methods for a given tenant.

    Used by both `list_pos_methods` (POS / despacho — authenticated, all groups)
    and the public-restaurant endpoint (anonymous customer checkout, where
    `exclude_cartera=True` filters out credit groups since walk-in customers
    can't accrue cartera).

    See warocol.com#610.
    """
    async with get_db_connection(use_transaction=False) as conn:
        groups = await conn.fetch(
            """
            SELECT id, name, slug, triggers_cartera, gl_account_code
            FROM payment_method_groups
            WHERE (tenant_id IS NULL OR tenant_id = $1) AND is_active = true
            ORDER BY sort_order, name
            """,
            tenant_id,
        )

        methods = await conn.fetch(
            """
            SELECT id, group_id, name, gl_account_code
            FROM payment_methods
            WHERE tenant_id = $1 AND is_active = true
            ORDER BY sort_order, name
            """,
            tenant_id,
        )

    methods_by_group: dict = {}
    for m in methods:
        gid = str(m["group_id"])
        if gid not in methods_by_group:
            methods_by_group[gid] = []
        methods_by_group[gid].append({
            "id": str(m["id"]),
            "name": m["name"],
            "glAccountCode": m["gl_account_code"],
        })

    data = [
        {
            "id": str(g["id"]),
            "name": g["name"],
            "slug": g["slug"],
            "triggersCartera": g["triggers_cartera"],
            "glAccountCode": g["gl_account_code"],
            "methods": methods_by_group.get(str(g["id"]), []),
        }
        for g in groups
        if not (exclude_cartera and g["triggers_cartera"])
    ]
    return {"success": True, "data": data}


async def list_pos_methods(request: Request) -> dict:
    """
    POS / despacho consumption endpoint — returns active groups (global +
    tenant's custom) with their active methods nested inside. Ordered by
    sort_order.
    """
    session = require_valid_session(request)
    return await _list_payment_methods_by_tenant_id(
        session.tenant_id, exclude_cartera=False
    )


async def list_public_methods_by_tenant_slug(tenant_slug: str) -> dict:
    """
    Public endpoint helper — resolves `tenant_slug` to a tenant_id via
    `tenant_public_profiles`, then returns the same shape as `list_pos_methods`
    but with `exclude_cartera=True` enforced server-side (anonymous customers
    can't use credit).

    Raises NotFoundError if the slug doesn't match an active profile.
    warocol.com#610.
    """
    async with get_db_connection(use_transaction=False) as conn:
        row = await conn.fetchrow(
            """
            SELECT tenant_id
            FROM tenant_public_profiles
            WHERE slug = $1 AND is_active = true
            """,
            tenant_slug,
        )
    if not row:
        raise NotFoundError("Restaurant not found or not active")

    return await _list_payment_methods_by_tenant_id(
        row["tenant_id"], exclude_cartera=True
    )

"""
Admin Ingredients Router — global base ingredient catalog management.

router: /admin/ingredients

Provides superuser-only endpoints to create and manage the global ingredient
catalog (tenant_id IS NULL, parent_id IS NULL). These are the base definitions
that all tenant restaurants use to create their own variants.

Python 3.9 safe: Optional[X] from typing, no X | None syntax.
"""
import asyncpg
import logging
from typing import Optional, List, Dict, Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Path, Request
from pydantic import BaseModel, Field

from app.core.middleware import require_valid_session
from app.database import get_db_connection

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/ingredients", tags=["Admin Ingredients"])


# ── Pydantic models ─────────────────────────────────────────────────────────
# Python 3.9 safe: Optional[X] from typing, no X | None syntax

class GlobalIngredientCreate(BaseModel):
    """Body for POST /admin/ingredients — create a global base ingredient."""
    name: str = Field(..., min_length=1, max_length=255)
    unit: str = Field(..., description="gr | ml | kg | und | lt")
    type: str = Field(default="food", description="food | service | supply")
    category: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = Field(None, max_length=1024)


class GlobalIngredientUpdate(BaseModel):
    """Body for PATCH /admin/ingredients/{id} — name/category/description only."""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    category: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = Field(None, max_length=1024)


# ── Superuser check helper ───────────────────────────────────────────────────

async def _require_superuser(conn, user_id: UUID, tenant_id: Optional[UUID]) -> None:
    """
    Raise 403 unless the caller has role='superuser' in their tenant.
    Pattern from api_tokens_service.py:99.
    """
    if not tenant_id:
        raise HTTPException(status_code=403, detail="Superuser access required")
    row = await conn.fetchrow(
        "SELECT role FROM tenant_members WHERE user_id = $1 AND tenant_id = $2",
        user_id, tenant_id
    )
    if not row or row["role"] != "superuser":
        raise HTTPException(status_code=403, detail="Superuser access required")


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("")
async def list_global_bases(
    request: Request,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=500),
    search: Optional[str] = Query(default=None),
    type: Optional[str] = Query(default=None, description="food | service | supply"),
):
    """
    List all global base ingredients (tenant_id IS NULL AND parent_id IS NULL).
    Includes computed variant_count per base.
    Superuser only.
    """
    session = require_valid_session(request)

    async with get_db_connection(use_transaction=False) as conn:
        await _require_superuser(conn, session.user_id, session.tenant_id)

        params: List[Any] = []
        where_clauses = [
            "i.tenant_id IS NULL",
            "i.parent_id IS NULL",
        ]
        param_idx = 1

        if search:
            where_clauses.append(
                f"(LOWER(i.name) LIKE LOWER(${param_idx}) OR LOWER(i.description) LIKE LOWER(${param_idx}))"
            )
            params.append(f"%{search}%")
            param_idx += 1

        if type:
            where_clauses.append(f"LOWER(i.type) = LOWER(${param_idx})")
            params.append(type)
            param_idx += 1

        where_sql = " AND ".join(where_clauses)

        count_row = await conn.fetchrow(
            f"SELECT COUNT(*) FROM ingredients i WHERE {where_sql}",
            *params
        )
        total = count_row["count"]

        offset = (page - 1) * limit
        params.extend([limit, offset])

        rows = await conn.fetch(
            f"""
            SELECT
                i.id, i.name, i.unit, i.type, i.category, i.description,
                i.created_at, i.updated_at,
                (SELECT COUNT(*) FROM ingredients v WHERE v.parent_id = i.id) AS variant_count
            FROM ingredients i
            WHERE {where_sql}
            ORDER BY i.name ASC
            LIMIT ${param_idx} OFFSET ${param_idx + 1}
            """,
            *params
        )

        data = [
            {
                "id": str(row["id"]),
                "name": row["name"],
                "unit": row["unit"],
                "type": row["type"],
                "category": row["category"],
                "description": row["description"],
                "variant_count": row["variant_count"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
            }
            for row in rows
        ]

        return {"success": True, "total": total, "page": page, "limit": limit, "data": data}


@router.post("", status_code=201)
async def create_global_base(
    request: Request,
    body: GlobalIngredientCreate,
):
    """
    Create a new global base ingredient (tenant_id = NULL, parent_id = NULL).
    Returns 409 if a base with the same name already exists.
    Superuser only.
    """
    session = require_valid_session(request)

    async with get_db_connection() as conn:
        await _require_superuser(conn, session.user_id, session.tenant_id)

        try:
            row = await conn.fetchrow(
                """
                INSERT INTO ingredients (name, unit, type, category, description, tenant_id, parent_id)
                VALUES ($1, $2, $3, $4, $5, NULL, NULL)
                RETURNING id, name, unit, type, category, description, created_at
                """,
                body.name, body.unit, body.type, body.category, body.description
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(
                status_code=409,
                detail="A base ingredient with this name already exists"
            )

        return {
            "success": True,
            "data": {
                "id": str(row["id"]),
                "name": row["name"],
                "unit": row["unit"],
                "type": row["type"],
                "category": row["category"],
                "description": row["description"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            }
        }


@router.patch("/{ingredient_id}")
async def update_global_base(
    request: Request,
    ingredient_id: UUID = Path(...),
    body: GlobalIngredientUpdate = ...,
):
    """
    Update name, category, or description of a global base ingredient.
    Unit and type are intentionally excluded — locked after creation.
    Returns 404 if not found or not a global base, 409 on duplicate name.
    Superuser only.
    """
    session = require_valid_session(request)

    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=422, detail="No fields provided to update")

    async with get_db_connection() as conn:
        await _require_superuser(conn, session.user_id, session.tenant_id)

        # Verify it's a global base
        existing = await conn.fetchrow(
            "SELECT id FROM ingredients WHERE id = $1 AND tenant_id IS NULL AND parent_id IS NULL",
            ingredient_id
        )
        if not existing:
            raise HTTPException(status_code=404, detail="Global base ingredient not found")

        set_clauses = []
        params: List[Any] = []
        idx = 1

        for field in ("name", "category", "description"):
            if field in updates:
                set_clauses.append(f"{field} = ${idx}")
                params.append(updates[field])
                idx += 1

        set_clauses.append(f"updated_at = NOW()")
        params.append(ingredient_id)

        try:
            row = await conn.fetchrow(
                f"""
                UPDATE ingredients
                SET {', '.join(set_clauses)}
                WHERE id = ${idx}
                RETURNING id, name, unit, type, category, description, updated_at
                """,
                *params
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(
                status_code=409,
                detail="A base ingredient with this name already exists"
            )

        return {
            "success": True,
            "data": {
                "id": str(row["id"]),
                "name": row["name"],
                "unit": row["unit"],
                "type": row["type"],
                "category": row["category"],
                "description": row["description"],
                "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
            }
        }


@router.get("/{ingredient_id}/variants")
async def get_variants_across_tenants(
    request: Request,
    ingredient_id: UUID = Path(..., description="Global base ingredient ID"),
):
    """
    List all variants of a global base across all tenants.
    Shows tenant_name (not raw tenant_id) for privacy.
    Superuser only.
    """
    session = require_valid_session(request)

    async with get_db_connection(use_transaction=False) as conn:
        await _require_superuser(conn, session.user_id, session.tenant_id)

        # Verify it's a global base
        base = await conn.fetchrow(
            "SELECT id, name, unit FROM ingredients WHERE id = $1 AND tenant_id IS NULL AND parent_id IS NULL",
            ingredient_id
        )
        if not base:
            raise HTTPException(status_code=404, detail="Global base ingredient not found")

        rows = await conn.fetch(
            """
            SELECT
                i.id, i.name, i.unit, i.type,
                t.name AS tenant_name
            FROM ingredients i
            LEFT JOIN tenants t ON i.tenant_id = t.id
            WHERE i.parent_id = $1
            ORDER BY i.name ASC
            """,
            ingredient_id
        )

        return {
            "success": True,
            "base": {
                "id": str(base["id"]),
                "name": base["name"],
                "unit": base["unit"],
            },
            "total": len(rows),
            "data": [
                {
                    "id": str(row["id"]),
                    "name": row["name"],
                    "unit": row["unit"],
                    "type": row["type"],
                    "tenant_name": row["tenant_name"] or "Sin nombre",
                }
                for row in rows
            ]
        }

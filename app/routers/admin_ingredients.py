"""
Admin Ingredients Router — hierarchy CRUD endpoints (issue #259)

Part of Epic #257 — Ingredient Global Hierarchy — Base-Variant Catalog.
Depends on #258 (ingredient_global_hierarchy table).

Prefix: /admin/ingredients
Auth:   require_valid_session (valid session = admin access, consistent with billing admin)

Immutability rule:
  - Global ingredients are NEVER edited or deleted — catalog only grows.
  - The only mutable operation is assigning/removing hierarchy rows.

Endpoints:
  GET  /admin/ingredients                  — list all global ingredients with hierarchy info
  GET  /admin/ingredients/{id}/variants    — list variants of a base ingredient
  POST /admin/ingredients                  — create new global ingredient (fuzzy guard, force=True to bypass)
  POST /admin/ingredients/{id}/set-base    — assign ingredient as variant of a base
  DELETE /admin/ingredients/{id}/set-base  — remove hierarchy link (ingredient stays intact)
"""
import logging
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.core.middleware import require_valid_session
from app.database import get_db_connection

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/ingredients", tags=["Ingredients Admin"])


# ── Pydantic models ───────────────────────────────────────────────────────────
# Python 3.9 safe: Optional[X] from typing, no X | None syntax

class IngredientAdminRow(BaseModel):
    id: str
    name: str
    unit: Optional[str] = None
    category: Optional[str] = None
    hierarchy_base_id: Optional[str] = None
    hierarchy_base_name: Optional[str] = None
    hierarchy_variant_count: int = 0


class CreateIngredientRequest(BaseModel):
    name: str
    unit: Optional[str] = None
    category: Optional[str] = None
    force: bool = False  # bypass fuzzy conflict and confirm creation
    base_id: Optional[str] = None  # if set: creates hierarchy row in same transaction


class SetBaseRequest(BaseModel):
    base_id: str


class ValidateBaseRequest(BaseModel):
    name: str


class SimilarIngredient(BaseModel):
    id: str
    name: str
    score: float


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("")
async def list_global_ingredients(
    request: Request,
    search: Optional[str] = Query(default=None, description="Filter by name (case-insensitive)"),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=100, ge=1, le=1000),
    bases_only: bool = Query(default=False, description="When true, exclude variant ingredients from results"),
) -> Dict[str, Any]:
    """
    List all global ingredients with hierarchy metadata.

    Returns base_id and base_name for variants, and variant_count for bases.
    All 2,291 global ingredients are always returned — visibility rule: no filtering
    by hierarchy status (all visible to all restaurants).
    """
    require_valid_session(request)

    offset = (page - 1) * limit
    params: List[Any] = []
    where_clause = "WHERE i.tenant_id IS NULL"

    if search:
        params.append(f"%{search}%")
        where_clause += f" AND i.name ILIKE ${len(params)}"

    if bases_only:
        where_clause += " AND NOT EXISTS (SELECT 1 FROM ingredient_global_hierarchy WHERE variant_id = i.id)"

    query = f"""
        SELECT
            i.id::text,
            i.name,
            i.unit,
            i.type,
            i.category,
            hb.base_id::text              AS hierarchy_base_id,
            bi.name                        AS hierarchy_base_name,
            COALESCE(vc.cnt, 0)::int      AS hierarchy_variant_count
        FROM ingredients i
        LEFT JOIN ingredient_global_hierarchy hb ON hb.variant_id = i.id
        LEFT JOIN ingredients bi ON bi.id = hb.base_id
        LEFT JOIN (
            SELECT base_id, COUNT(*)::int AS cnt
            FROM ingredient_global_hierarchy
            GROUP BY base_id
        ) vc ON vc.base_id = i.id
        {where_clause}
        ORDER BY i.name
        LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
    """
    params.extend([limit, offset])

    count_query = f"""
        SELECT COUNT(*) FROM ingredients i
        {where_clause}
    """
    count_params = params[:-2]  # exclude limit/offset

    async with get_db_connection() as conn:
        rows = await conn.fetch(query, *params)
        total = await conn.fetchval(count_query, *count_params)

    return {
        "success": True,
        "data": [dict(r) for r in rows],
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "pages": (total + limit - 1) // limit if total else 0,
        },
    }


@router.get("/{ingredient_id}/variants")
async def list_ingredient_variants(
    ingredient_id: UUID,
    request: Request,
) -> Dict[str, Any]:
    """
    List all variants of a base ingredient via ingredient_global_hierarchy.
    Uses the hierarchy table — not the deprecated parent_id column.
    """
    require_valid_session(request)

    async with get_db_connection() as conn:
        # Verify base exists and is global
        base = await conn.fetchrow(
            "SELECT id::text, name, unit FROM ingredients WHERE id = $1 AND tenant_id IS NULL",
            ingredient_id,
        )
        if not base:
            raise HTTPException(status_code=404, detail="Global ingredient not found")

        rows = await conn.fetch(
            """
            SELECT i.id::text, i.name, i.unit, i.category
            FROM ingredients i
            JOIN ingredient_global_hierarchy h ON h.variant_id = i.id
            WHERE h.base_id = $1
            ORDER BY i.name
            """,
            ingredient_id,
        )

    return {
        "success": True,
        "base": dict(base),
        "data": [dict(r) for r in rows],
        "count": len(rows),
    }


@router.post("/validate-base", status_code=200)
async def validate_base_name(
    body: ValidateBaseRequest,
    request: Request,
) -> Dict[str, Any]:
    """
    Validates a potential new base ingredient name before creation.

    1. pg_trgm similarity check at threshold 0.4 against global catalog.
    2. If similar candidates found → Gemini semantic duplicate check.
    3. Returns:
       - verdict "suggest": Gemini detected a semantic duplicate, includes `suggested` ingredient.
       - verdict "create":  No duplicate found, safe to auto-create.
    """
    require_valid_session(request)

    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Name cannot be empty")

    async with get_db_connection() as conn:
        similar_rows = await conn.fetch(
            """
            SELECT id::text, name, unit, similarity(name, $1)::float AS score
            FROM ingredients
            WHERE tenant_id IS NULL AND similarity(name, $1) > 0.4
            ORDER BY score DESC
            LIMIT 5
            """,
            name,
        )

    if not similar_rows:
        return {"verdict": "create", "similar": [], "suggested": None}

    candidates = [
        {"id": r["id"], "name": r["name"], "unit": r["unit"]}
        for r in similar_rows
    ]

    from app.services.gemini_service import check_name_semantic_duplicate
    gemini_result = await check_name_semantic_duplicate(name, candidates)

    if gemini_result.get("is_duplicate") and gemini_result.get("best_match_id"):
        match_id = gemini_result["best_match_id"]
        suggested = next((c for c in candidates if c["id"] == match_id), candidates[0])
        return {
            "verdict": "suggest",
            "suggested": suggested,
            "similar": candidates,
            "reason": gemini_result.get("reason", ""),
        }

    return {"verdict": "create", "similar": candidates, "suggested": None}


@router.post("", status_code=201)
async def create_global_ingredient(
    body: CreateIngredientRequest,
    request: Request,
) -> Dict[str, Any]:
    """
    Create a new global ingredient.

    Step 1 (if force=False): fuzzy similarity check at threshold 0.4 via pg_trgm.
    If similar names found → returns HTTP 409 with candidates list.
    Client must re-send with force=True to confirm creation despite similarity.

    Immutability rule: no endpoint to edit or delete global ingredients.
    """
    require_valid_session(request)

    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Ingredient name cannot be empty")

    # Validate base_id if provided
    base_uuid = None
    if body.base_id:
        try:
            base_uuid = UUID(body.base_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="base_id must be a valid UUID")

    async with get_db_connection() as conn:
        if not body.force:
            similar_rows = await conn.fetch(
                """
                SELECT id::text, name, similarity(name, $1)::float AS score
                FROM ingredients
                WHERE tenant_id IS NULL AND similarity(name, $1) > 0.4
                ORDER BY score DESC
                LIMIT 5
                """,
                name,
            )
            if similar_rows:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "status": "conflict",
                        "message": "Similar ingredients already exist. Send force=true to create anyway.",
                        "similar": [
                            {"id": r["id"], "name": r["name"], "score": round(r["score"], 3)}
                            for r in similar_rows
                        ],
                    },
                )

        # Resolve unit: use request unit, or auto-fill from base if base_id provided
        unit_to_use = body.unit
        base_row = None
        if base_uuid:
            base_row = await conn.fetchrow(
                "SELECT id, name, unit FROM ingredients WHERE id = $1 AND tenant_id IS NULL",
                base_uuid,
            )
            if not base_row:
                raise HTTPException(
                    status_code=422,
                    detail="base_id not found or not a global ingredient",
                )
            if not unit_to_use:
                unit_to_use = base_row["unit"]
            elif unit_to_use != base_row["unit"]:
                raise HTTPException(
                    status_code=422,
                    detail=f"Unit mismatch: requested '{unit_to_use}', base is '{base_row['unit']}'. "
                           "Either omit unit (auto-filled from base) or match the base unit.",
                )

        async with conn.transaction():
            new_row = await conn.fetchrow(
                """
                INSERT INTO ingredients (name, unit, category, tenant_id)
                VALUES ($1, $2, $3, NULL)
                RETURNING id::text, name, unit, category
                """,
                name,
                unit_to_use,
                body.category,
            )

            hierarchy_id = None
            if base_uuid:
                h_row = await conn.fetchrow(
                    """
                    INSERT INTO ingredient_global_hierarchy (base_id, variant_id)
                    VALUES ($1, $2::uuid)
                    RETURNING id
                    """,
                    base_uuid,
                    new_row["id"],
                )
                hierarchy_id = str(h_row["id"])

    result = dict(new_row)
    if hierarchy_id:
        result["hierarchy_id"] = hierarchy_id
        result["hierarchy_base_id"] = str(base_uuid)
        result["hierarchy_base_name"] = base_row["name"]

    return {
        "success": True,
        "data": result,
    }


@router.post("/{ingredient_id}/set-base", status_code=200)
async def assign_base(
    ingredient_id: UUID,
    body: SetBaseRequest,
    request: Request,
) -> Dict[str, Any]:
    """
    Assign a base to an ingredient (make it a variant).

    Validates:
    - Both ingredients are global (tenant_id IS NULL)
    - Units match (skipped if either has NULL unit)
    - Ingredient does not already have a base (409 if so)

    Idempotent: ON CONFLICT(variant_id) → 409 with clear message.
    """
    require_valid_session(request)

    try:
        base_uuid = UUID(body.base_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="base_id must be a valid UUID")

    if ingredient_id == base_uuid:
        raise HTTPException(status_code=422, detail="An ingredient cannot be its own base")

    async with get_db_connection() as conn:
        ingredient = await conn.fetchrow(
            "SELECT id, name, unit FROM ingredients WHERE id = $1 AND tenant_id IS NULL",
            ingredient_id,
        )
        if not ingredient:
            raise HTTPException(status_code=404, detail="Global ingredient not found")

        base = await conn.fetchrow(
            "SELECT id, name, unit FROM ingredients WHERE id = $1 AND tenant_id IS NULL",
            base_uuid,
        )
        if not base:
            raise HTTPException(status_code=404, detail="Base ingredient not found or not a global ingredient")

        # Unit match validation — only block when both units are set and differ
        if ingredient["unit"] and base["unit"] and ingredient["unit"] != base["unit"]:
            raise HTTPException(
                status_code=422,
                detail=f"Unit mismatch: ingredient is '{ingredient['unit']}', base is '{base['unit']}'. "
                       "Both must have the same unit to form a hierarchy.",
            )

        # Insert — UNIQUE(variant_id) prevents a variant from having two bases
        existing = await conn.fetchrow(
            "SELECT id, base_id::text FROM ingredient_global_hierarchy WHERE variant_id = $1",
            ingredient_id,
        )
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"Ingredient already has a base assigned (hierarchy row id={existing['id']}). "
                       "Remove the existing base first with DELETE /{id}/set-base.",
            )

        row = await conn.fetchrow(
            """
            INSERT INTO ingredient_global_hierarchy (base_id, variant_id)
            VALUES ($1, $2)
            RETURNING id, base_id::text, variant_id::text
            """,
            base_uuid,
            ingredient_id,
        )

    return {
        "success": True,
        "data": {
            "hierarchy_id": row["id"],
            "variant_id": row["variant_id"],
            "variant_name": ingredient["name"],
            "base_id": row["base_id"],
            "base_name": base["name"],
        },
    }


@router.delete("/{ingredient_id}/set-base", status_code=200)
async def remove_base(
    ingredient_id: UUID,
    request: Request,
) -> Dict[str, Any]:
    """
    Remove the hierarchy relationship for an ingredient.

    Deletes from ingredient_global_hierarchy only — the ingredient itself is untouched.
    Returns removed=true if a row was deleted, removed=false if none existed.
    """
    require_valid_session(request)

    async with get_db_connection() as conn:
        deleted = await conn.fetchrow(
            """
            DELETE FROM ingredient_global_hierarchy
            WHERE variant_id = $1
            RETURNING id, base_id::text, variant_id::text
            """,
            ingredient_id,
        )

    return {
        "success": True,
        "removed": deleted is not None,
        "data": dict(deleted) if deleted else None,
    }

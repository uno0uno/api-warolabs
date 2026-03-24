#!/usr/bin/env python3
"""
Migration: ingredient_global_hierarchy — table + data population

Creates the ingredient_global_hierarchy table and populates it with inferred
base→variant relationships from the 2,280 global ingredients using prefix matching.

No existing tables are modified. Zero impact on ingredients table data.

Part of issue #258 / epic #257.

Usage:
  python3 migration_ingredient_hierarchy.py            # dry-run (default)
  python3 migration_ingredient_hierarchy.py --dry-run  # explicit dry-run
  python3 migration_ingredient_hierarchy.py --apply    # write to DB
"""
import argparse
import asyncio
from typing import Dict, List, Optional, Set, Tuple

import asyncpg
from app.config import settings


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class Ingredient:
    def __init__(self, id: str, name: str, unit: str) -> None:
        self.id = id
        self.name = name
        self.unit = unit


class RelationshipCandidate:
    def __init__(self, base: Ingredient, variant: Ingredient) -> None:
        self.base = base
        self.variant = variant


# ---------------------------------------------------------------------------
# Phase A — Table creation (always runs, idempotent)
# ---------------------------------------------------------------------------

async def create_table(conn: asyncpg.Connection) -> None:
    print("🔧 Phase A — Creating table ingredient_global_hierarchy (if not exists)...")

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS ingredient_global_hierarchy (
            id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            base_id    UUID NOT NULL REFERENCES ingredients(id) ON DELETE CASCADE,
            variant_id UUID NOT NULL REFERENCES ingredients(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(variant_id)
        )
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_igh_base_id
            ON ingredient_global_hierarchy(base_id)
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_igh_variant_id
            ON ingredient_global_hierarchy(variant_id)
    """)

    print("  ✅ Table and indexes ready")


# ---------------------------------------------------------------------------
# Phase B — Candidate computation (no writes, runs in both modes)
# ---------------------------------------------------------------------------

async def load_global_ingredients(conn: asyncpg.Connection) -> List[Ingredient]:
    rows = await conn.fetch("""
        SELECT id::text, name, unit
        FROM ingredients
        WHERE tenant_id IS NULL
        ORDER BY name
    """)
    return [Ingredient(r["id"], r["name"], r["unit"]) for r in rows]


async def load_existing_variants(conn: asyncpg.Connection) -> Set[str]:
    """Load variant_ids already in the hierarchy table (for idempotency)."""
    rows = await conn.fetch("""
        SELECT variant_id::text FROM ingredient_global_hierarchy
    """)
    return {r["variant_id"] for r in rows}


async def load_existing_bases(conn: asyncpg.Connection) -> Set[str]:
    """Load base_ids already inserted as variants (they cannot become bases)."""
    rows = await conn.fetch("""
        SELECT variant_id::text FROM ingredient_global_hierarchy
    """)
    return {r["variant_id"] for r in rows}


def compute_candidates(
    ingredients: List[Ingredient],
    existing_variant_ids: Set[str],
    existing_base_disqualified_ids: Set[str],
) -> Tuple[List[RelationshipCandidate], List[Tuple[str, List[str]]], List[Tuple[str, str, str]]]:
    """
    Apply prefix-matching logic to find base→variant relationships.

    Safeguards:
    - Base name must be >= 3 chars
    - Variant name must start with base name + " " (case-insensitive)
    - Same unit required
    - Base must not itself be a variant in ingredient_global_hierarchy
    - Only one candidate base per variant (ambiguous = skip)
    - Variant must not already be in the table (idempotency)

    Returns:
        unambiguous: list of RelationshipCandidate ready to insert
        ambiguous: list of (variant_name, [candidate_base_names]) to log
        unit_mismatch: list of (variant_name, base_name, unit_info) — logged for info
    """
    # Build lookup by lowercase name for fast prefix search
    by_name_lower: Dict[str, Ingredient] = {
        ing.name.lower(): ing for ing in ingredients
    }

    unambiguous: List[RelationshipCandidate] = []
    ambiguous: List[Tuple[str, List[str]]] = []
    unit_mismatch_log: List[Tuple[str, str, str]] = []

    for variant in ingredients:
        # Skip if already registered as a variant
        if variant.id in existing_variant_ids:
            continue

        v_lower = variant.name.lower()
        candidates_same_unit: List[Ingredient] = []
        candidates_diff_unit: List[Ingredient] = []

        for base in ingredients:
            if base.id == variant.id:
                continue

            b_lower = base.name.lower()

            # Minimum base name length
            if len(b_lower) < 3:
                continue

            # Prefix match: variant starts with base + space
            if not v_lower.startswith(b_lower + " "):
                continue

            # Base must not be a registered variant (idempotency safeguard)
            if base.id in existing_base_disqualified_ids:
                continue

            if base.unit == variant.unit:
                candidates_same_unit.append(base)
            else:
                candidates_diff_unit.append(base)

        # Log unit mismatches (informational only)
        for base in candidates_diff_unit:
            unit_mismatch_log.append((
                variant.name,
                base.name,
                f"variant={variant.unit} base={base.unit}",
            ))

        # Resolve same-unit candidates
        if len(candidates_same_unit) == 0:
            pass  # no match — skip silently
        elif len(candidates_same_unit) == 1:
            unambiguous.append(RelationshipCandidate(candidates_same_unit[0], variant))
        else:
            ambiguous.append((variant.name, [b.name for b in candidates_same_unit]))

    return unambiguous, ambiguous, unit_mismatch_log


# ---------------------------------------------------------------------------
# Phase C — Dry-run output
# ---------------------------------------------------------------------------

def print_dry_run(
    unambiguous: List[RelationshipCandidate],
    ambiguous: List[Tuple[str, List[str]]],
    unit_mismatch_log: List[Tuple[str, str, str]],
    existing_count: int,
) -> None:
    print(f"\n[DRY RUN] Would insert {len(unambiguous)} relationships "
          f"({existing_count} already exist, skipped):\n")

    for rel in unambiguous:
        print(f"  {rel.base.name} → {rel.variant.name}  [{rel.base.unit}]")

    if ambiguous:
        print(f"\n  SKIPPED (ambiguous) — {len(ambiguous)} items with multiple candidate bases:")
        for variant_name, candidates in ambiguous:
            print(f"    {variant_name!r} — candidates: {candidates}")

    if unit_mismatch_log:
        print(f"\n  SKIPPED (unit mismatch) — {len(unit_mismatch_log)} potential pairs ignored:")
        for variant_name, base_name, units in unit_mismatch_log:
            print(f"    {variant_name!r} vs {base_name!r}  ({units})")

    print(f"\n[DRY RUN] Summary:")
    print(f"  Would insert:  {len(unambiguous)}")
    print(f"  Ambiguous:     {len(ambiguous)}")
    print(f"  Unit mismatch: {len(unit_mismatch_log)}")
    print(f"  Already exist: {existing_count}")
    print("\nRun with --apply to write to the database.")


# ---------------------------------------------------------------------------
# Phase D — Apply (transaction, ON CONFLICT DO NOTHING)
# ---------------------------------------------------------------------------

async def apply_relationships(
    conn: asyncpg.Connection,
    unambiguous: List[RelationshipCandidate],
) -> int:
    inserted = 0

    async with conn.transaction():
        for rel in unambiguous:
            result = await conn.execute("""
                INSERT INTO ingredient_global_hierarchy (base_id, variant_id)
                VALUES ($1::uuid, $2::uuid)
                ON CONFLICT (variant_id) DO NOTHING
            """, rel.base.id, rel.variant.id)
            # result is like "INSERT 0 1" or "INSERT 0 0"
            if result.endswith(" 1"):
                inserted += 1

    return inserted


async def print_verification(conn: asyncpg.Connection) -> None:
    count_row = await conn.fetchrow("SELECT COUNT(*) as n FROM ingredient_global_hierarchy")
    total = count_row["n"]
    print(f"\n  ✅ Total rows in ingredient_global_hierarchy: {total}")

    samples = await conn.fetch("""
        SELECT b.name as base, v.name as variant, h.created_at
        FROM ingredient_global_hierarchy h
        JOIN ingredients b ON b.id = h.base_id
        JOIN ingredients v ON v.id = h.variant_id
        ORDER BY b.name, v.name
        LIMIT 10
    """)
    print("\n  Sample relationships:")
    for row in samples:
        print(f"    {row['base']} → {row['variant']}")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def run_migration(dry_run: bool) -> None:
    conn: Optional[asyncpg.Connection] = None
    try:
        conn = await asyncpg.connect(
            host=settings.db_host,
            port=settings.db_port,
            user=settings.db_user,
            password=settings.db_password,
            database=settings.db_name,
        )

        # Phase A — always create table (idempotent)
        await create_table(conn)

        # Phase B — compute candidates
        print("\n🔍 Phase B — Loading global ingredients and computing candidates...")
        ingredients = await load_global_ingredients(conn)
        print(f"  Loaded {len(ingredients)} global ingredients")

        existing_variant_ids: Set[str] = await load_existing_variants(conn)
        existing_base_disqualified_ids: Set[str] = await load_existing_bases(conn)
        existing_count = len(existing_variant_ids)
        print(f"  Already in hierarchy: {existing_count} rows")

        unambiguous, ambiguous, unit_mismatch_log = compute_candidates(
            ingredients,
            existing_variant_ids,
            existing_base_disqualified_ids,
        )

        if dry_run:
            # Phase C — dry-run output
            print_dry_run(unambiguous, ambiguous, unit_mismatch_log, existing_count)
        else:
            # Phase D — apply
            print(f"\n📝 Phase D — Applying {len(unambiguous)} relationships...")
            if ambiguous:
                print(f"  ⚠️  Skipping {len(ambiguous)} ambiguous variants (logged below)")
            if unit_mismatch_log:
                print(f"  ⚠️  Skipping {len(unit_mismatch_log)} unit-mismatch pairs")

            inserted = await apply_relationships(conn, unambiguous)
            print(f"  ✅ Inserted: {inserted}  (skipped via ON CONFLICT: {len(unambiguous) - inserted})")

            await print_verification(conn)

            if ambiguous:
                print(f"\n  SKIPPED (ambiguous) — require manual assignment:")
                for variant_name, candidates in ambiguous:
                    print(f"    {variant_name!r} — candidates: {candidates}")

            print("\n✅ Migration complete.")

    except Exception as e:
        print(f"\n❌ Error: {e}")
        raise
    finally:
        if conn:
            await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Create ingredient_global_hierarchy table and populate from global ingredients."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Show proposed relationships without writing (default)",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Write relationships to the database (runs in a transaction)",
    )
    args = parser.parse_args()

    dry_run = not args.apply
    asyncio.run(run_migration(dry_run=dry_run))

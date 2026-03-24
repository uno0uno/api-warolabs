#!/usr/bin/env python3
"""
Migration: ingredient_global_hierarchy — add generic base ingredients

Creates missing generic base ingredients (Café, Crema de Leche, Leche, etc.)
and connects existing brand-level bases as their variants in ingredient_global_hierarchy.

Zero deletions. Only INSERTs into ingredients + ingredient_global_hierarchy.

Part of issue #258 / epic #257.

Usage:
  python3 migration_ingredient_hierarchy_generics.py            # dry-run (default)
  python3 migration_ingredient_hierarchy_generics.py --dry-run  # explicit dry-run
  python3 migration_ingredient_hierarchy_generics.py --apply    # write to DB
"""
import argparse
import asyncio
from typing import Dict, List, Optional, Tuple

import asyncpg
from app.config import settings


# ---------------------------------------------------------------------------
# Definition: new generic ingredients to create
# (name, unit, category)
# ---------------------------------------------------------------------------

NEW_GENERICS: List[Tuple[str, str, str]] = [
    ("Café",          "gr",  "Cafe"),
    ("Crema de Leche","ml",  "Lácteos"),
    ("Leche",         "ml",  "Lácteos"),
    ("Yogurt",        "ml",  "Lácteos"),
    ("Harina",        "gr",  "Harinas"),
    ("Avena",         "gr",  "Granos"),
    ("Salsa",         "ml",  "Salsas"),
    ("Queso",         "gr",  "Lácteos"),
]

# ---------------------------------------------------------------------------
# Definition: mappings generic_name → [brand base names to make its variants]
# ---------------------------------------------------------------------------

MAPPINGS: Dict[str, List[str]] = {
    "Café": [
        "Cafe Juan Valdez",
        "Café Juan Valdez",
        "Café Oma Tostado Molido",
        "Cafe Sello Rojo",
        "Café Sello Rojo",
        "Café Tostao Tostado Y Molido",
    ],
    "Crema de Leche": [
        "Crema de Leche Alpina",
        "Crema De Leche Parmalat Semientera",
    ],
    "Leche": [
        "Leche Colanta Semidescremada",
        "Leche Condensada",
        "Leche de Coco",
        "Leche Deslactosada",
        "Leche Slight Descremada",
    ],
    "Yogurt": [
        "Yogurt Finesse Natural Cereal",
        "Yogurt Griego Alpina Natural",
        "Yogurt Griego San Martin Natural",
        "Yogurt Tapioka",
    ],
    "Harina": [
        "Harina Arepasan De Maíz",
        "Harina de Arroz",
        "Harina de Garbanzo",
        "Harina de Maíz Amarillo",
        "Harina de Trigo Haz de Oros",
        "Harina Pan Maíz Blanco",
    ],
    "Avena": [
        "Avena en Hojuelas",
        "Avena Finesse Vaso",
    ],
    "Salsa": [
        "Salsa de Soya",
        "Salsa de Tomate Fruco",
        "Salsa Para Pastas Frescampo Tomate",
        "Salsa Picante",
        "Salsa Soya Lee Kum Kee Sazonadora",
    ],
    "Queso": [
        "Queso Campesino Colanta",
        "Queso Costeño",
        "Queso Dona Leche Campesino",
        "Queso Flor Del Caqueta Doble Crema",
    ],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def load_name_to_id(conn: asyncpg.Connection) -> Dict[str, str]:
    rows = await conn.fetch(
        "SELECT id::text, name FROM ingredients WHERE tenant_id IS NULL"
    )
    return {r["name"]: r["id"] for r in rows}


async def load_existing_variant_ids(conn: asyncpg.Connection) -> set:
    rows = await conn.fetch("SELECT variant_id::text FROM ingredient_global_hierarchy")
    return {r["variant_id"] for r in rows}


# ---------------------------------------------------------------------------
# Phase A — Insert generic ingredients (idempotent via INSERT ... ON CONFLICT)
# ---------------------------------------------------------------------------

async def insert_generics(
    conn: asyncpg.Connection,
    dry_run: bool,
) -> List[str]:
    """Insert missing generic ingredients. Returns list of names that will be / were inserted."""
    to_insert: List[Tuple[str, str, str]] = []

    for name, unit, category in NEW_GENERICS:
        existing = await conn.fetchval(
            "SELECT id FROM ingredients WHERE name = $1 AND tenant_id IS NULL",
            name,
        )
        if existing is None:
            to_insert.append((name, unit, category))

    if dry_run:
        return [n for n, _, _ in to_insert]

    async with conn.transaction():
        for name, unit, category in to_insert:
            await conn.execute("""
                INSERT INTO ingredients (name, unit, category, ai_generated)
                VALUES ($1, $2, $3, false)
                ON CONFLICT (name) DO NOTHING
            """, name, unit, category)

    return [n for n, _, _ in to_insert]


# ---------------------------------------------------------------------------
# Phase B — Insert hierarchy mappings (generic → brand bases)
# ---------------------------------------------------------------------------

async def build_hierarchy_inserts(
    conn: asyncpg.Connection,
    name_to_id: Dict[str, str],
    existing_variant_ids: set,
) -> Tuple[List[Tuple[str, str, str, str]], List[Tuple[str, str]]]:
    """
    Returns:
      to_insert: list of (generic_name, brand_name, generic_id, brand_id)
      skipped:   list of (brand_name, reason)
    """
    to_insert: List[Tuple[str, str, str, str]] = []
    skipped: List[Tuple[str, str]] = []

    for generic_name, brand_names in MAPPINGS.items():
        generic_id = name_to_id.get(generic_name)
        if generic_id is None:
            skipped.append((generic_name, "generic not found in DB"))
            continue

        for brand_name in brand_names:
            brand_id = name_to_id.get(brand_name)
            if brand_id is None:
                skipped.append((brand_name, "brand ingredient not found in DB"))
                continue
            if brand_id in existing_variant_ids:
                skipped.append((brand_name, "already has a parent in hierarchy"))
                continue
            to_insert.append((generic_name, brand_name, generic_id, brand_id))

    return to_insert, skipped


async def apply_hierarchy_inserts(
    conn: asyncpg.Connection,
    to_insert: List[Tuple[str, str, str, str]],
) -> int:
    inserted = 0
    async with conn.transaction():
        for _, _, generic_id, brand_id in to_insert:
            result = await conn.execute("""
                INSERT INTO ingredient_global_hierarchy (base_id, variant_id)
                VALUES ($1::uuid, $2::uuid)
                ON CONFLICT (variant_id) DO NOTHING
            """, generic_id, brand_id)
            if result.endswith(" 1"):
                inserted += 1
    return inserted


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def print_dry_run(
    new_generics: List[str],
    to_insert: List[Tuple[str, str, str, str]],
    skipped: List[Tuple[str, str]],
) -> None:
    print(f"\n[DRY RUN] New generic ingredients to create: {len(new_generics)}")
    for name in new_generics:
        unit = next(u for n, u, _ in NEW_GENERICS if n == name)
        cat = next(c for n, _, c in NEW_GENERICS if n == name)
        print(f"  + {name}  [{unit}]  ({cat})")

    print(f"\n[DRY RUN] Hierarchy rows to insert: {len(to_insert)}")
    for generic_name, brand_name, _, _ in to_insert:
        print(f"  {generic_name} → {brand_name}")

    if skipped:
        print(f"\n[DRY RUN] Skipped: {len(skipped)}")
        for name, reason in skipped:
            print(f"  ⚠️  {name!r} — {reason}")

    print(f"\n[DRY RUN] Summary:")
    print(f"  New ingredients: {len(new_generics)}")
    print(f"  New hierarchy rows: {len(to_insert)}")
    print(f"  Skipped: {len(skipped)}")
    print("\nRun with --apply to write to the database.")


async def print_verification(conn: asyncpg.Connection) -> None:
    total = await conn.fetchval("SELECT COUNT(*) FROM ingredient_global_hierarchy")
    print(f"\n  ✅ Total rows in ingredient_global_hierarchy: {total}")


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

        print("🔍 Loading current DB state...")
        name_to_id = await load_name_to_id(conn)
        existing_variant_ids = await load_existing_variant_ids(conn)
        print(f"  {len(name_to_id)} global ingredients loaded")
        print(f"  {len(existing_variant_ids)} existing hierarchy rows")

        # Phase A — generic ingredients
        print("\n📦 Phase A — Generic ingredients")
        new_generics = await insert_generics(conn, dry_run=True)  # always preview first

        # After dry-run insert, refresh name_to_id so Phase B can find the new IDs
        if not dry_run:
            await insert_generics(conn, dry_run=False)
            name_to_id = await load_name_to_id(conn)
            existing_variant_ids = await load_existing_variant_ids(conn)

        # Phase B — hierarchy mappings
        # In dry-run: simulate the new generics being present so counts are accurate
        if dry_run:
            for name, unit, category in NEW_GENERICS:
                if name not in name_to_id:
                    name_to_id[name] = f"simulated-{name}"

        print("📦 Phase B — Hierarchy mappings")
        to_insert, skipped = await build_hierarchy_inserts(conn, name_to_id, existing_variant_ids)

        if dry_run:
            print_dry_run(new_generics, to_insert, skipped)
        else:
            print(f"  Creating {len(new_generics)} generic ingredients...")
            print(f"  Inserting {len(to_insert)} hierarchy rows...")

            inserted = await apply_hierarchy_inserts(conn, to_insert)
            print(f"  ✅ Hierarchy rows inserted: {inserted}")

            if skipped:
                print(f"\n  ⚠️  Skipped {len(skipped)}:")
                for name, reason in skipped:
                    print(f"    {name!r} — {reason}")

            await print_verification(conn)
            print("\n✅ Migration complete.")

    except Exception as e:
        print(f"\n❌ Error: {e}")
        raise
    finally:
        if conn:
            await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Add generic base ingredients and hierarchy mappings."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True)
    mode.add_argument("--apply",   action="store_true", default=False)
    args = parser.parse_args()

    asyncio.run(run_migration(dry_run=not args.apply))

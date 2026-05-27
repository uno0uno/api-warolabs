"""
Backfill tables.code from existing names (warocol.com#927).

Uses the same inference as POS tableShortId. Resolves tenant-level collisions
with single-letter suffixes. Bar tables get BAR.

Requires DB access (SSH tunnel or local):
    ssh -L 5432:localhost:5432 warolabs -N &
    cd api_warocol.com && python scripts/backfill_table_codes.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.utils.table_code import infer_table_code, resolve_unique_code  # noqa: E402

load_dotenv(ROOT / ".env")

DB_CONFIG = {
    "host": "localhost",
    "port": int(os.getenv("NUXT_PRIVATE_DB_PORT", "5432")),
    "user": os.getenv("NUXT_PRIVATE_DB_USER"),
    "password": os.getenv("NUXT_PRIVATE_DB_PASSWORD"),
    "database": os.getenv("NUXT_PRIVATE_DB_NAME"),
}


async def main() -> None:
    conn = await asyncpg.connect(**DB_CONFIG)
    try:
        rows = await conn.fetch(
            """
            SELECT id, tenant_id, name, is_bar, code
            FROM tables
            WHERE deleted_at IS NULL
            ORDER BY tenant_id, created_at, id
            """
        )

        by_tenant: dict[str, set[str]] = {}
        updated = 0

        for row in rows:
            tenant_key = str(row["tenant_id"])
            used = by_tenant.setdefault(tenant_key, set())

            if row["code"]:
                used.add(str(row["code"]).upper())
                continue

            if row["is_bar"]:
                proposed = "BAR"
            else:
                proposed = infer_table_code(row["name"])

            code = resolve_unique_code(proposed, used)
            used.add(code.upper())

            await conn.execute(
                "UPDATE tables SET code = $1 WHERE id = $2",
                code,
                row["id"],
            )
            updated += 1
            print(f"  {row['name']!r} -> {code}")

        print(f"\nDone. Updated {updated} table(s).")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

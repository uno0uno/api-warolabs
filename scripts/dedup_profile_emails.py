"""
Deduplicate profile rows that share lower(trim(email)) and normalize emails to lowercase.

Usage (requires DB tunnel):
    cd api_warocol.com
    python scripts/dedup_profile_emails.py           # dry-run (default)
    python scripts/dedup_profile_emails.py --apply   # execute changes

Steps:
  A) Merge duplicate groups (keeper = highest score: tenant_members, sessions, FKs, name)
  B) Lowercase remaining profile / tenant_invitations / leads emails
  C) Create unique index on lower(trim(email)) if missing
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from urllib.parse import urlparse, unquote
from uuid import UUID

import asyncpg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.email_utils import normalize_email  # noqa: E402

load_dotenv(ROOT / ".env")

SCORE_TABLES = (
    ("sessions", "user_id"),
    ("magic_tokens", "user_id"),
    ("tenant_invitations", "user_id"),
    ("tenant_members", "user_id"),
    ("orders", "user_id"),
    ("table_sessions", "opened_by_user_id"),
    ("pos_carts", "user_id"),
    ("leads", "profile_id"),
)


def _db_config() -> dict:
    url = os.getenv("DATABASE_URL", "")
    if url:
        p = urlparse(url)
        return {
            "host": p.hostname or "localhost",
            "port": p.port or 5432,
            "user": p.username,
            "password": unquote(p.password or ""),
            "database": p.path.lstrip("/"),
        }
    return {
        "host": "localhost",
        "port": int(os.getenv("NUXT_PRIVATE_DB_PORT", "5432")),
        "user": os.getenv("NUXT_PRIVATE_DB_USER"),
        "password": os.getenv("NUXT_PRIVATE_DB_PASSWORD"),
        "database": os.getenv("NUXT_PRIVATE_DB_NAME"),
    }


async def _fk_columns(conn: asyncpg.Connection) -> list[tuple[str, str]]:
    rows = await conn.fetch(
        """
        SELECT tc.table_name, kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
          AND tc.table_schema = kcu.table_schema
        JOIN information_schema.constraint_column_usage ccu
          ON ccu.constraint_name = tc.constraint_name
          AND ccu.table_schema = tc.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_schema = 'public'
          AND ccu.table_name = 'profile'
          AND ccu.column_name = 'id'
        ORDER BY tc.table_name, kcu.column_name
        """
    )
    return [(r["table_name"], r["column_name"]) for r in rows]


async def _profile_score(conn: asyncpg.Connection, profile_id: UUID) -> tuple:
    row = await conn.fetchrow(
        """
        SELECT
          p.id,
          p.email,
          p.name,
          p.created_at,
          (SELECT count(*) FROM tenant_members tm
           WHERE tm.user_id = p.id AND tm.is_active) AS active_members,
          (SELECT count(*) FROM sessions s
           WHERE s.user_id = p.id AND s.is_active AND s.expires_at > now()) AS active_sessions,
          (SELECT count(*) FROM sessions s WHERE s.user_id = p.id) AS total_sessions
        FROM profile p
        WHERE p.id = $1
        """,
        profile_id,
    )
    fk_total = 0
    for table, col in SCORE_TABLES:
        n = await conn.fetchval(
            f'SELECT count(*) FROM "{table}" WHERE "{col}" = $1',  # noqa: S608
            profile_id,
        )
        fk_total += n or 0
    name_len = len((row["name"] or "").strip())
    # Higher is better; created_at older wins on tie via neg timestamp
    return (
        row["active_members"] or 0,
        row["active_sessions"] or 0,
        row["total_sessions"] or 0,
        fk_total,
        name_len,
        -row["created_at"].timestamp(),
        row["email"],
        row["id"],
    )


async def _session_report(conn: asyncpg.Connection, ids: list[UUID]) -> None:
    rows = await conn.fetch(
        """
        SELECT user_id,
               count(*) AS total,
               count(*) FILTER (WHERE is_active AND expires_at > now()) AS active
        FROM sessions
        WHERE user_id = ANY($1::uuid[])
        GROUP BY user_id
        """,
        ids,
    )
    print("  sessions:")
    if not rows:
        print("    (none)")
        return
    for r in rows:
        print(f"    {r['user_id']}: total={r['total']} active={r['active']}")


async def _merge_dupe_into_keeper(
    conn: asyncpg.Connection,
    keeper_id: UUID,
    dupe_id: UUID,
    fk_cols: list[tuple[str, str]],
    dry_run: bool,
) -> int:
    moved = 0
    for table, col in fk_cols:
        if table == "tenant_members" and col == "user_id":
            dup_members = await conn.fetch(
                "SELECT id, tenant_id FROM tenant_members WHERE user_id = $1",
                dupe_id,
            )
            for m in dup_members:
                exists = await conn.fetchval(
                    "SELECT id FROM tenant_members WHERE user_id = $1 AND tenant_id = $2",
                    keeper_id,
                    m["tenant_id"],
                )
                if exists:
                    if not dry_run:
                        await conn.execute(
                            "DELETE FROM tenant_members WHERE id = $1", m["id"]
                        )
                    print(f"    tenant_members: delete dupe row {m['id']} (keeper has tenant)")
                else:
                    if not dry_run:
                        await conn.execute(
                            "UPDATE tenant_members SET user_id = $1 WHERE id = $2",
                            keeper_id,
                            m["id"],
                        )
                    print(f"    tenant_members: move {m['id']} -> keeper")
                    moved += 1
            continue

        count = await conn.fetchval(
            f'SELECT count(*) FROM "{table}" WHERE "{col}" = $1',  # noqa: S608
            dupe_id,
        )
        if count:
            print(f"    {table}.{col}: move {count} rows")
            if not dry_run:
                await conn.execute(
                    f'UPDATE "{table}" SET "{col}" = $1 WHERE "{col}" = $2',  # noqa: S608
                    keeper_id,
                    dupe_id,
                )
            moved += count

    inv_count = await conn.fetchval(
        "SELECT count(*) FROM tenant_invitations WHERE user_id = $1", dupe_id
    )
    if inv_count:
        print(f"    tenant_invitations: update {inv_count} row(s)")
        if not dry_run:
            norm = await conn.fetchval(
                "SELECT lower(trim(email)) FROM profile WHERE id = $1", keeper_id
            )
            await conn.execute(
                """
                UPDATE tenant_invitations
                SET user_id = $1, email = $2
                WHERE user_id = $3
                """,
                keeper_id,
                norm,
                dupe_id,
            )

    keeper_name = await conn.fetchval("SELECT name FROM profile WHERE id = $1", keeper_id)
    dupe_name = await conn.fetchval("SELECT name FROM profile WHERE id = $1", dupe_id)
    if not (keeper_name or "").strip() and (dupe_name or "").strip():
        print(f"    profile.name: copy from dupe")
        if not dry_run:
            await conn.execute(
                "UPDATE profile SET name = $1 WHERE id = $2",
                dupe_name,
                keeper_id,
            )

    print(f"    DELETE profile {dupe_id}")
    if not dry_run:
        await conn.execute("DELETE FROM profile WHERE id = $1", dupe_id)

    return moved


async def dedup_groups(conn: asyncpg.Connection, dry_run: bool) -> None:
    groups = await conn.fetch(
        """
        SELECT lower(trim(email)) AS norm, array_agg(id ORDER BY created_at) AS ids
        FROM profile
        GROUP BY 1
        HAVING count(*) > 1
        """
    )
    if not groups:
        print("No duplicate email groups.")
        return

    fk_cols = await _fk_columns(conn)
    print(f"Found {len(groups)} duplicate group(s), {len(fk_cols)} FK columns to profile.")

    for g in groups:
        ids = g["ids"]
        print(f"\n=== Group {g['norm']} ({len(ids)} profiles) ===")
        scores = []
        for pid in ids:
            scores.append(await _profile_score(conn, pid))
        scores.sort(reverse=True)
        keeper_score = scores[0]
        keeper_id = keeper_score[-1]
        print(f"  keeper: {keeper_score[-2]} ({keeper_id})")
        dupes = [s for s in scores[1:]]
        await _session_report(conn, ids)
        for dupe_score in dupes:
            dupe_id = dupe_score[-1]
            dupe_email = dupe_score[-2]
            print(f"  dupe: {dupe_email} ({dupe_id})")
            await _merge_dupe_into_keeper(conn, keeper_id, dupe_id, fk_cols, dry_run)

        norm_email = normalize_email(g["norm"])
        keeper_email = await conn.fetchval("SELECT email FROM profile WHERE id = $1", keeper_id)
        if keeper_email != norm_email:
            print(f"  UPDATE keeper email: {keeper_email} -> {norm_email}")
            if not dry_run:
                await conn.execute(
                    "UPDATE profile SET email = $1 WHERE id = $2",
                    norm_email,
                    keeper_id,
                )


async def lowercase_remaining(conn: asyncpg.Connection, dry_run: bool) -> None:
    for label, sql in (
        ("profile", "SELECT count(*) FROM profile WHERE email <> lower(trim(email))"),
        ("tenant_invitations", "SELECT count(*) FROM tenant_invitations WHERE email <> lower(trim(email))"),
        ("leads", "SELECT count(*) FROM leads WHERE email <> lower(trim(email))"),
    ):
        n = await conn.fetchval(sql)
        print(f"\n{label}: {n} row(s) to lowercase")
        if n and not dry_run:
            if label == "profile":
                await conn.execute(
                    "UPDATE profile SET email = lower(trim(email)) WHERE email <> lower(trim(email))"
                )
            elif label == "tenant_invitations":
                await conn.execute(
                    "UPDATE tenant_invitations SET email = lower(trim(email)) WHERE email <> lower(trim(email))"
                )
            else:
                await conn.execute(
                    "UPDATE leads SET email = lower(trim(email)) WHERE email <> lower(trim(email))"
                )


async def ensure_unique_index(conn: asyncpg.Connection, dry_run: bool) -> None:
    exists = await conn.fetchval(
        """
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public' AND indexname = 'profile_email_lower_unique'
        """
    )
    if exists:
        print("\nIndex profile_email_lower_unique already exists.")
        return
    print("\nCREATE UNIQUE INDEX profile_email_lower_unique ON profile (lower(trim(email)))")
    if not dry_run:
        await conn.execute(
            "CREATE UNIQUE INDEX profile_email_lower_unique ON profile (lower(trim(email)))"
        )


async def main(apply: bool) -> None:
    dry_run = not apply
    if dry_run:
        print("=== DRY RUN (pass --apply to execute) ===\n")
    else:
        print("=== APPLYING CHANGES ===\n")

    conn = await asyncpg.connect(**_db_config())
    try:
        async with conn.transaction():
            await dedup_groups(conn, dry_run)
            await lowercase_remaining(conn, dry_run)
            await ensure_unique_index(conn, dry_run)
            if dry_run:
                raise DryRunRollback()
    except DryRunRollback:
        print("\n=== Dry run complete (rolled back) ===")
    finally:
        await conn.close()


class DryRunRollback(Exception):
    pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dedup profile emails and normalize to lowercase")
    parser.add_argument("--apply", action="store_true", help="Execute changes (default: dry-run)")
    args = parser.parse_args()
    asyncio.run(main(args.apply))

"""Canonical storefront slug assignment + contingency aliases (#832)."""
from __future__ import annotations

from typing import Any, Dict, Optional
from uuid import UUID

from app.core.public_slug import (
    is_onboarding_provisional_slug,
    raise_opaque_identity_conflict,
    slugify_business_name,
)


async def assert_business_identity_available(
    conn,
    *,
    business_name: str,
    slug: str,
    exclude_tenant_id: Optional[UUID] = None,
) -> None:
    """Ensure name + slug are free. Opaque 409 on any collision."""
    name_row = await conn.fetchrow(
        """
        SELECT id
        FROM tenants
        WHERE lower(trim(name)) = lower(trim($1))
          AND ($2::uuid IS NULL OR id IS DISTINCT FROM $2)
        LIMIT 1
        """,
        business_name,
        exclude_tenant_id,
    )
    if name_row:
        raise_opaque_identity_conflict()

    slug_tenant = await conn.fetchrow(
        """
        SELECT id
        FROM tenants
        WHERE slug = $1
          AND ($2::uuid IS NULL OR id IS DISTINCT FROM $2)
        LIMIT 1
        """,
        slug,
        exclude_tenant_id,
    )
    if slug_tenant:
        raise_opaque_identity_conflict()

    slug_public = await conn.fetchrow(
        """
        SELECT tenant_id
        FROM tenant_public_profiles
        WHERE slug = $1
          AND ($2::uuid IS NULL OR tenant_id IS DISTINCT FROM $2)
        LIMIT 1
        """,
        slug,
        exclude_tenant_id,
    )
    if slug_public:
        raise_opaque_identity_conflict()

    # Table may not exist until migration is applied — treat missing as no aliases.
    try:
        alias = await conn.fetchrow(
            """
            SELECT tenant_id
            FROM tenant_public_slug_aliases
            WHERE alias_slug = $1
              AND ($2::uuid IS NULL OR tenant_id IS DISTINCT FROM $2)
            LIMIT 1
            """,
            slug,
            exclude_tenant_id,
        )
    except Exception:
        alias = None
    if alias:
        raise_opaque_identity_conflict()


async def assign_name_based_storefront_slug(
    conn,
    *,
    tenant_id: UUID,
    business_name: str,
) -> str:
    """
    Set tenants.slug + public profile slug from business name.

    If the previous slug was provisional `onboarding-*`, store it as a
    contingency alias (redirect only for that legacy case).
    """
    slug = slugify_business_name(business_name)
    await assert_business_identity_available(
        conn,
        business_name=business_name,
        slug=slug,
        exclude_tenant_id=tenant_id,
    )

    previous = await conn.fetchrow(
        "SELECT slug FROM tenants WHERE id = $1 FOR UPDATE",
        tenant_id,
    )
    previous_slug = previous["slug"] if previous else None

    await conn.execute(
        """
        UPDATE tenants
        SET name = $2,
            slug = $3
        WHERE id = $1
        """,
        tenant_id,
        business_name,
        slug,
    )
    await conn.execute(
        """
        INSERT INTO tenant_public_profiles (tenant_id, slug, display_name)
        VALUES ($1, $2, $3)
        ON CONFLICT (tenant_id) DO UPDATE
            SET slug = EXCLUDED.slug,
                display_name = EXCLUDED.display_name,
                updated_at = now()
        """,
        tenant_id,
        slug,
        business_name,
    )

    if (
        is_onboarding_provisional_slug(previous_slug)
        and previous_slug
        and previous_slug != slug
    ):
        try:
            await conn.execute(
                """
                INSERT INTO tenant_public_slug_aliases (alias_slug, tenant_id)
                VALUES ($1, $2)
                ON CONFLICT (alias_slug) DO NOTHING
                """,
                previous_slug,
                tenant_id,
            )
        except Exception:
            # Migration not applied yet — assignment still succeeds.
            pass

    return slug


async def get_canonical_slug_if_alias(slug: str) -> Optional[str]:
    """Return canonical public slug when `slug` is a contingency alias."""
    from app.database import get_db_connection

    try:
        async with get_db_connection() as conn:
            row = await conn.fetchrow(
                """
                SELECT tpp.slug AS canonical_slug
                FROM tenant_public_slug_aliases a
                JOIN tenant_public_profiles tpp ON tpp.tenant_id = a.tenant_id
                WHERE a.alias_slug = $1
                LIMIT 1
                """,
                slug,
            )
            if row and row["canonical_slug"] and row["canonical_slug"] != slug:
                return row["canonical_slug"]
    except Exception:
        return None
    return None


async def migrate_provisional_onboarding_slugs(conn) -> Dict[str, Any]:
    """
    One-shot contingency: rename safe `onboarding-*` tenants to name-based slugs
    and keep the old slug as alias. Skips collisions (opaque leave-as-is).
    """
    rows = await conn.fetch(
        """
        SELECT id, name, slug
        FROM tenants
        WHERE slug LIKE 'onboarding-%'
        ORDER BY created_at NULLS LAST
        """
    )
    migrated = 0
    skipped = 0
    for row in rows:
        name = (row["name"] or "").strip()
        if not name or name.lower() == "negocio pendiente":
            skipped += 1
            continue
        try:
            await assign_name_based_storefront_slug(
                conn,
                tenant_id=row["id"],
                business_name=name,
            )
            migrated += 1
        except Exception:
            skipped += 1
    return {"migrated": migrated, "skipped": skipped, "total": len(rows)}

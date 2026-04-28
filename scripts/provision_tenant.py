"""
provision_tenant.py — Crea un tenant y sus usuarios directamente en DB.

Requiere SSH tunnel activo:
    ssh -L 5432:localhost:5432 warolabs -N &

Uso:
    cd api_warocol.com
    python scripts/provision_tenant.py

Lo que hace:
    1. Crea el perfil de nadia (superuser) si no existe
    2. Crea el perfil de anderson si no existe
    3. Crea el tenant "Natural Food" (slug unico)
    4. Crea tenant_public_profiles
    5. Agrega nadia como superuser
    6. Agrega anderson como admin
    7. Llama a seed_tenant_accounts() — 52 cuentas PUC colombiano
"""

import asyncio
import os
import re
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

# Cargar .env desde la raiz del proyecto
load_dotenv(Path(__file__).parent.parent / ".env")

# ── Configuracion ─────────────────────────────────────────────────────────────

TENANT_NAME = "Natural Food"

SUPERUSER_EMAIL = "nadia.sanchezm@hotmail.com"
SUPERUSER_NAME = "Nadia Sanchez"

ADMIN_EMAIL = "anderson.electronico@gmail.com"
ADMIN_NAME = "Anderson"

# ── DB connection (SSH tunnel → localhost) ─────────────────────────────────────

DB_CONFIG = {
    "host": "localhost",  # siempre localhost via tunnel
    "port": int(os.getenv("NUXT_PRIVATE_DB_PORT", "5432")),
    "user": os.getenv("NUXT_PRIVATE_DB_USER"),
    "password": os.getenv("NUXT_PRIVATE_DB_PASSWORD"),
    "database": os.getenv("NUXT_PRIVATE_DB_NAME"),
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def generate_slug(name: str) -> str:
    slug = name.lower()
    slug = re.sub(r"[áàäâã]", "a", slug)
    slug = re.sub(r"[éèëê]", "e", slug)
    slug = re.sub(r"[íìïî]", "i", slug)
    slug = re.sub(r"[óòöôõ]", "o", slug)
    slug = re.sub(r"[úùüû]", "u", slug)
    slug = re.sub(r"ñ", "n", slug)
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s-]+", "-", slug)
    return slug.strip("-")


def email_to_username(email: str) -> str:
    local = email.split("@")[0]
    username = re.sub(r"[^a-z0-9._-]", "", local.lower())
    return username[:30]


async def get_or_create_profile(conn, email: str, name: str) -> str:
    """Retorna el user_id (UUID como str). Crea el perfil si no existe."""
    existing = await conn.fetchval(
        "SELECT id FROM profile WHERE email = $1", email
    )
    if existing:
        print(f"  [OK] Perfil ya existe: {email} → {existing}")
        return str(existing)

    username = email_to_username(email)

    # Asegurar username unico
    base_username = username
    counter = 1
    while await conn.fetchval("SELECT 1 FROM profile WHERE user_name = $1", username):
        username = f"{base_username}{counter}"
        counter += 1

    user_id = await conn.fetchval(
        """
        INSERT INTO profile (email, name, user_name, created_at, updated_at)
        VALUES ($1, $2, $3, NOW(), NOW())
        RETURNING id
        """,
        email,
        name,
        username,
    )
    print(f"  [CREADO] Perfil: {email} → {user_id} (username: {username})")
    return str(user_id)


async def provision():
    print("Conectando a DB via tunnel (localhost:5432)...")
    conn = await asyncpg.connect(**DB_CONFIG)

    try:
        async with conn.transaction():

            # ── 1 & 2. Perfiles ──────────────────────────────────────────────
            print("\n[1/5] Perfiles de usuario")
            superuser_id = await get_or_create_profile(conn, SUPERUSER_EMAIL, SUPERUSER_NAME)
            admin_id = await get_or_create_profile(conn, ADMIN_EMAIL, ADMIN_NAME)

            # ── 3. Tenant ────────────────────────────────────────────────────
            print(f"\n[2/5] Tenant: {TENANT_NAME}")
            existing_tenant = await conn.fetchrow(
                "SELECT id, slug FROM tenants WHERE name = $1", TENANT_NAME
            )
            if existing_tenant:
                tenant_id = str(existing_tenant["id"])
                tenant_slug = existing_tenant["slug"]
                print(f"  [OK] Tenant ya existe: {tenant_id} (slug: {tenant_slug})")
            else:
                base_slug = generate_slug(TENANT_NAME)
                slug = base_slug
                counter = 1
                while await conn.fetchval("SELECT 1 FROM tenants WHERE slug = $1", slug):
                    slug = f"{base_slug}-{counter}"
                    counter += 1

                tenant_row = await conn.fetchrow(
                    """
                    INSERT INTO tenants (id, name, slug, created_at)
                    VALUES (gen_random_uuid(), $1, $2, NOW())
                    RETURNING id, slug
                    """,
                    TENANT_NAME,
                    slug,
                )
                tenant_id = str(tenant_row["id"])
                tenant_slug = tenant_row["slug"]
                print(f"  [CREADO] Tenant: {tenant_id} (slug: {tenant_slug})")

            # ── 4. tenant_public_profiles ────────────────────────────────────
            print("\n[3/5] tenant_public_profiles")
            existing_pub = await conn.fetchval(
                "SELECT 1 FROM tenant_public_profiles WHERE tenant_id = $1", tenant_id
            )
            if existing_pub:
                print("  [OK] tenant_public_profiles ya existe")
            else:
                await conn.execute(
                    """
                    INSERT INTO tenant_public_profiles
                        (tenant_id, display_name, slug,
                         is_active, is_manually_open, welcome_email_sent,
                         tables_enabled, comandas_enabled, kds_enabled)
                    VALUES ($1, $2, $3, true, false, false, false, false, false)
                    """,
                    tenant_id,
                    TENANT_NAME,
                    tenant_slug,
                )
                print("  [CREADO] tenant_public_profiles")

            # ── 5. tenant_members ────────────────────────────────────────────
            print("\n[4/5] Miembros del tenant")

            for user_id, email, role in [
                (superuser_id, SUPERUSER_EMAIL, "superuser"),
                (admin_id, ADMIN_EMAIL, "admin"),
            ]:
                existing_member = await conn.fetchval(
                    "SELECT 1 FROM tenant_members WHERE tenant_id = $1 AND user_id = $2",
                    tenant_id,
                    user_id,
                )
                if existing_member:
                    print(f"  [OK] {email} ya es miembro ({role})")
                else:
                    await conn.execute(
                        """
                        INSERT INTO tenant_members (id, tenant_id, user_id, role)
                        VALUES (gen_random_uuid(), $1, $2, $3)
                        """,
                        tenant_id,
                        user_id,
                        role,
                    )
                    print(f"  [CREADO] {email} agregado como {role}")

            # ── 6. Seed PUC ──────────────────────────────────────────────────
            print("\n[5/5] Sembrando cuentas PUC colombiano...")
            existing_accounts = await conn.fetchval(
                "SELECT COUNT(*) FROM tenant_accounts WHERE tenant_id = $1", tenant_id
            )
            if existing_accounts and existing_accounts > 0:
                print(f"  [OK] Ya tiene {existing_accounts} cuentas — omitiendo seed")
            else:
                await conn.execute("SELECT seed_tenant_accounts($1)", tenant_id)
                total = await conn.fetchval(
                    "SELECT COUNT(*) FROM tenant_accounts WHERE tenant_id = $1", tenant_id
                )
                print(f"  [CREADO] {total} cuentas PUC sembradas")

        print("\n[LISTO] Provision completada exitosamente.")
        print(f"  Tenant:    {TENANT_NAME} ({tenant_slug})")
        print(f"  Superuser: {SUPERUSER_EMAIL}")
        print(f"  Admin:     {ADMIN_EMAIL}")

    finally:
        await conn.close()


if __name__ == "__main__":
    missing = [k for k, v in DB_CONFIG.items() if v is None]
    if missing:
        print(f"ERROR: Faltan variables de entorno: {missing}")
        print("Asegurate de tener el .env cargado correctamente.")
        sys.exit(1)

    asyncio.run(provision())

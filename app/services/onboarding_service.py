import hashlib
import hmac
from typing import Any, Optional
from uuid import UUID, uuid4

from fastapi import HTTPException

from app.config import settings
from app.core.internal_roles import LEGACY_INTERNAL_TEAM_ROLES
from app.core.onboarding_access import next_step_for_state
from app.models.onboarding import OnboardingStatus, OnboardingStatusResponse

MAX_EMAIL_REQUESTS = 5
MAX_IP_REQUESTS = 20
MAX_VERIFY_ATTEMPTS = 5


def _credential_hash(email: str, kind: str, value: str) -> str:
    payload = f"onboarding:{kind}:{email}:{value}".encode("utf-8")
    return hmac.new(settings.auth_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


async def store_registration_challenge(
    conn,
    *,
    email: str,
    token: str,
    code: str,
    request_ip: Optional[str],
    user_agent: Optional[str],
) -> bool:
    """Persist a pre-user challenge for an address without an active tenant."""
    limits = await conn.fetchrow(
        """
        SELECT
            COUNT(*) FILTER (WHERE normalized_email = $1) AS email_count,
            COUNT(*) FILTER (WHERE request_ip = $2::inet) AS ip_count
        FROM onboarding_email_challenges
        WHERE created_at > NOW() - INTERVAL '15 minutes'
          AND (normalized_email = $1 OR ($2::inet IS NOT NULL AND request_ip = $2::inet))
        """,
        email,
        request_ip,
    )
    email_count = int((limits or {}).get("email_count") or 0)
    ip_count = int((limits or {}).get("ip_count") or 0)
    if email_count >= MAX_EMAIL_REQUESTS or ip_count >= MAX_IP_REQUESTS:
        raise HTTPException(status_code=429, detail="Too many verification requests")

    await conn.execute(
        """
        UPDATE onboarding_email_challenges
        SET consumed_at = COALESCE(consumed_at, NOW())
        WHERE normalized_email = $1
          AND consumed_at IS NULL
        """,
        email,
    )
    await conn.execute(
        """
        INSERT INTO onboarding_email_challenges (
            normalized_email, token_hash, code_hash, request_ip,
            user_agent, expires_at
        )
        VALUES ($1, $2, $3, $4::inet, $5, NOW() + INTERVAL '15 minutes')
        """,
        email,
        _credential_hash(email, "token", token),
        _credential_hash(email, "code", code),
        request_ip,
        user_agent,
    )
    return True


async def _identity_for_tenant(conn, user_id: UUID, tenant_id: UUID) -> Optional[dict[str, Any]]:
    row = await conn.fetchrow(
        """
        SELECT p.id AS user_id, p.email, p.name,
               p.created_at AS user_created_at,
               t.id AS tenant_id, t.name AS tenant_name, t.slug AS tenant_slug,
               t.lifecycle_status, o.state AS onboarding_state,
               o.email_verified_at
        FROM profile p
        JOIN tenants t ON t.id = $2
        LEFT JOIN tenant_onboarding o
          ON o.tenant_id = t.id AND o.owner_user_id = p.id
        WHERE p.id = $1
        LIMIT 1
        """,
        user_id,
        tenant_id,
    )
    if not row:
        return None
    result = dict(row)
    result["next_step"] = next_step_for_state(result.get("onboarding_state"))
    return result


async def complete_registration(
    conn,
    *,
    email: str,
    credential: str,
    kind: str,
) -> Optional[dict[str, Any]]:
    """Consume a challenge and atomically create or resume pending identity."""
    if kind not in {"token", "code"}:
        raise ValueError("Unsupported onboarding credential kind")

    hash_column = "token_hash" if kind == "token" else "code_hash"
    challenge = await conn.fetchrow(
        f"""
        SELECT id, consumed_at, completed_user_id, completed_tenant_id
        FROM onboarding_email_challenges
        WHERE normalized_email = $1
          AND {hash_column} = $2
          AND expires_at > NOW()
          AND failed_attempts < $3
        ORDER BY created_at DESC
        LIMIT 1
        FOR UPDATE
        """,
        email,
        _credential_hash(email, kind, credential),
        MAX_VERIFY_ATTEMPTS,
    )
    if not challenge:
        await conn.execute(
            """
            UPDATE onboarding_email_challenges
            SET failed_attempts = LEAST(failed_attempts + 1, $2)
            WHERE id = (
                SELECT id
                FROM onboarding_email_challenges
                WHERE normalized_email = $1
                  AND consumed_at IS NULL
                  AND expires_at > NOW()
                ORDER BY created_at DESC
                LIMIT 1
            )
            """,
            email,
            MAX_VERIFY_ATTEMPTS,
        )
        return None

    if challenge.get("completed_user_id") and challenge.get("completed_tenant_id"):
        return await _identity_for_tenant(
            conn,
            challenge["completed_user_id"],
            challenge["completed_tenant_id"],
        )
    if challenge.get("consumed_at"):
        return None

    # Serialize all provisioning decisions for one normalized address. The DB
    # constraints remain the final protection if callers bypass this service.
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", email)

    active = await conn.fetchrow(
        """
        SELECT p.id AS user_id, p.email, p.name,
               p.created_at AS user_created_at,
               t.id AS tenant_id, t.name AS tenant_name, t.slug AS tenant_slug,
               t.lifecycle_status, NULL::text AS onboarding_state,
               NULL::timestamptz AS email_verified_at
        FROM profile p
        JOIN tenant_members tm ON tm.user_id = p.id
        JOIN tenants t ON t.id = tm.tenant_id
        WHERE lower(trim(p.email)) = $1
          AND tm.is_active = true
          AND tm.role = ANY($2::text[])
          AND t.lifecycle_status = 'active'
        ORDER BY tm.id
        LIMIT 1
        """,
        email,
        list(LEGACY_INTERNAL_TEAM_ROLES),
    )
    if active:
        identity = dict(active)
        identity["next_step"] = None
    else:
        pending = await conn.fetchrow(
            """
            SELECT p.id AS user_id, p.email, p.name,
                   p.created_at AS user_created_at,
                   t.id AS tenant_id, t.name AS tenant_name, t.slug AS tenant_slug,
                   t.lifecycle_status, o.state AS onboarding_state,
                   o.email_verified_at
            FROM profile p
            JOIN tenant_onboarding o ON o.owner_user_id = p.id
            JOIN tenants t ON t.id = o.tenant_id
            WHERE lower(trim(p.email)) = $1
              AND t.lifecycle_status = 'pending'
              AND o.state NOT IN ('setup_complete', 'cancelled')
            LIMIT 1
            """,
            email,
        )
        if pending:
            identity = dict(pending)
            identity["next_step"] = next_step_for_state(identity.get("onboarding_state"))
        else:
            profile = await conn.fetchrow(
                """
                INSERT INTO profile (email, created_at, updated_at)
                VALUES ($1, NOW(), NOW())
                ON CONFLICT ((lower(trim(email)))) DO UPDATE
                    SET email = EXCLUDED.email,
                        updated_at = NOW()
                RETURNING id, email, name, created_at
                """,
                email,
            )
            tenant_id = uuid4()
            slug = f"onboarding-{tenant_id.hex[:16]}"
            await conn.execute(
                """
                INSERT INTO tenants (id, name, slug, email, lifecycle_status, created_at)
                VALUES ($1, 'Negocio pendiente', $2, $3, 'pending', NOW())
                """,
                tenant_id,
                slug,
                email,
            )
            onboarding = await conn.fetchrow(
                """
                INSERT INTO tenant_onboarding (
                    tenant_id, owner_user_id, verified_email, state,
                    email_verified_at, created_at, updated_at
                )
                VALUES ($1, $2, $3, 'business_profile_pending', NOW(), NOW(), NOW())
                RETURNING state, email_verified_at
                """,
                tenant_id,
                profile["id"],
                email,
            )
            await conn.execute(
                """
                INSERT INTO tenant_members (id, tenant_id, user_id, role, is_active)
                VALUES (gen_random_uuid(), $1, $2, 'owner', false)
                """,
                tenant_id,
                profile["id"],
            )
            identity = {
                "user_id": profile["id"],
                "email": profile["email"],
                "name": profile.get("name"),
                "user_created_at": profile["created_at"],
                "tenant_id": tenant_id,
                "tenant_name": "Negocio pendiente",
                "tenant_slug": slug,
                "lifecycle_status": "pending",
                "onboarding_state": onboarding["state"],
                "email_verified_at": onboarding["email_verified_at"],
                "next_step": next_step_for_state(onboarding["state"]),
            }

    await conn.execute(
        """
        UPDATE onboarding_email_challenges
        SET consumed_at = COALESCE(consumed_at, NOW()),
            completed_user_id = $2,
            completed_tenant_id = $3
        WHERE id = $1
        """,
        challenge["id"],
        identity["user_id"],
        identity["tenant_id"],
    )
    return identity


async def get_status_for_tenant(conn, tenant_id: UUID) -> OnboardingStatusResponse:
    row = await conn.fetchrow(
        """
        SELECT t.id AS tenant_id, t.lifecycle_status,
               o.state, o.email_verified_at
        FROM tenants t
        LEFT JOIN tenant_onboarding o ON o.tenant_id = t.id
        WHERE t.id = $1
        """,
        tenant_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Onboarding not found")
    return OnboardingStatusResponse(
        data=OnboardingStatus(
            tenantId=row["tenant_id"],
            lifecycleStatus=row["lifecycle_status"],
            state=row.get("state"),
            nextStep=next_step_for_state(row.get("state")),
            emailVerifiedAt=row.get("email_verified_at"),
        )
    )

import hashlib
import hmac
import json
from typing import Any, Optional
from uuid import UUID, uuid4

from fastapi import HTTPException

from app.config import settings
from app.core.internal_roles import LEGACY_INTERNAL_TEAM_ROLES
from app.core.onboarding_access import next_step_for_state
from app.models.onboarding import (
    OnboardingBusinessProfileUpdate,
    OnboardingFinancialData,
    OnboardingFinancialResponse,
    OnboardingStatus,
    OnboardingStatusResponse,
)
from app.models.tenant_financial_profile import TenantFinancialProfile
from app.services import legal_service
from app.services import tenant_financial_profile_service as financial_service

MAX_EMAIL_REQUESTS = 5
MAX_IP_REQUESTS = 20
MAX_VERIFY_ATTEMPTS = 5
PRE_PAYMENT_FINANCIAL_STATES = frozenset({
    "business_profile_pending",
    "terms_pending",
    "starter_active",
    "payment_pending",
})


def _credential_hash(email: str, kind: str, value: str) -> str:
    payload = f"onboarding:{kind}:{email}:{value}".encode("utf-8")
    return hmac.new(settings.auth_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _opaque_token_hash(value: str) -> str:
    payload = f"onboarding:opaque-token:{value}".encode("utf-8")
    return hmac.new(settings.auth_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


async def store_registration_challenge(
    conn,
    *,
    email: str,
    token: str,
    code: str,
    request_ip: Optional[str],
    user_agent: Optional[str],
    phone_country_code: Optional[int] = None,
    phone_number: Optional[str] = None,
    consent: bool = False,
    business_name: Optional[str] = None,
    country_code: Optional[str] = None,
    base_currency_code: Optional[str] = None,
    source: Optional[str] = None,
    content: Optional[str] = None,
    campaign: Optional[str] = None,
    variant: Optional[str] = None,
) -> bool:
    """Persist a pre-user challenge for an address without an active tenant."""
    if consent is not True:
        raise HTTPException(status_code=422, detail="Registration consent is required")

    # Serialize the read/check/write sequence for every affected rate-limit
    # bucket. Sorting avoids deadlocks when requests share only one bucket.
    lock_keys = [f"onboarding-email:{email}"]
    if request_ip:
        lock_keys.append(f"onboarding-ip:{request_ip}")
    for lock_key in sorted(lock_keys):
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext($1))",
            lock_key,
        )

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
          AND purpose = 'registration'
          AND consumed_at IS NULL
        """,
        email,
    )
    await conn.execute(
        """
        INSERT INTO onboarding_email_challenges (
            normalized_email, token_hash, code_hash, opaque_token_hash,
            purpose, request_ip, user_agent, expires_at,
            phone_country_code, phone_number, consent_at, consent_version,
            business_name, country_code, base_currency_code,
            first_source, first_content, first_campaign, first_variant,
            last_source, last_content, last_campaign, last_variant
        )
        VALUES (
            $1, $2, $3, $4, 'registration', $5::inet, $6,
            NOW() + INTERVAL '15 minutes', $7, $8, NOW(),
            'self_service_registration_v1', $9, $10, $11,
            COALESCE((
                SELECT first_source FROM onboarding_email_challenges
                WHERE normalized_email = $1 AND purpose = 'registration'
                ORDER BY created_at ASC LIMIT 1
            ), $12),
            COALESCE((
                SELECT first_content FROM onboarding_email_challenges
                WHERE normalized_email = $1 AND purpose = 'registration'
                ORDER BY created_at ASC LIMIT 1
            ), $13),
            COALESCE((
                SELECT first_campaign FROM onboarding_email_challenges
                WHERE normalized_email = $1 AND purpose = 'registration'
                ORDER BY created_at ASC LIMIT 1
            ), $14),
            COALESCE((
                SELECT first_variant FROM onboarding_email_challenges
                WHERE normalized_email = $1 AND purpose = 'registration'
                ORDER BY created_at ASC LIMIT 1
            ), $15),
            $12, $13, $14, $15
        )
        """,
        email,
        _credential_hash(email, "token", token),
        _credential_hash(email, "code", code),
        _opaque_token_hash(token),
        request_ip,
        user_agent,
        phone_country_code,
        phone_number,
        business_name,
        country_code,
        base_currency_code,
        source,
        content,
        campaign,
        variant,
    )
    await conn.execute(
        """
        DELETE FROM onboarding_email_challenges
        WHERE created_at < NOW() - INTERVAL '24 hours'
        """
    )
    return True


async def get_resumable_registration_draft(conn, email: str) -> Optional[dict[str, Any]]:
    """Return the latest consented pre-verification draft without exposing secrets."""
    row = await conn.fetchrow(
        """
        SELECT phone_country_code, phone_number,
               business_name, country_code, base_currency_code,
               first_source, first_content, first_campaign, first_variant,
               last_source, last_content, last_campaign, last_variant
        FROM onboarding_email_challenges
        WHERE normalized_email = $1
          AND purpose = 'registration'
          AND consumed_at IS NULL
          AND completed_user_id IS NULL
          AND completed_tenant_id IS NULL
          AND consent_at IS NOT NULL
          AND created_at > NOW() - INTERVAL '24 hours'
          AND failed_attempts < $2
        ORDER BY created_at DESC
        LIMIT 1
        """,
        email,
        MAX_VERIFY_ATTEMPTS,
    )
    return dict(row) if row else None


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
    email: Optional[str],
    credential: str,
    kind: str,
    opaque_token: bool = False,
    legacy_only: bool = False,
) -> Optional[dict[str, Any]]:
    """Consume a challenge and atomically create or resume pending identity."""
    if kind not in {"token", "code"}:
        raise ValueError("Unsupported onboarding credential kind")

    challenge_fields = """
        id, normalized_email, consumed_at, completed_user_id, completed_tenant_id,
        phone_country_code, phone_number,
        business_name, country_code, base_currency_code,
        first_source, first_content, first_campaign, first_variant,
        last_source, last_content, last_campaign, last_variant
    """
    if opaque_token:
        if kind != "token":
            raise ValueError("Opaque lookup is only supported for registration tokens")
        challenge = await conn.fetchrow(
            f"""
            SELECT {challenge_fields}
            FROM onboarding_email_challenges
            WHERE purpose = 'registration'
              AND opaque_token_hash = $1
              AND expires_at > NOW()
              AND failed_attempts < $2
            LIMIT 1
            FOR UPDATE
            """,
            _opaque_token_hash(credential),
            MAX_VERIFY_ATTEMPTS,
        )
    else:
        if not email:
            raise ValueError("Email is required for code and legacy token verification")
        hash_column = "token_hash" if kind == "token" else "code_hash"
        legacy_filter = "AND opaque_token_hash IS NULL" if legacy_only else ""
        challenge = await conn.fetchrow(
            f"""
            SELECT {challenge_fields}
            FROM onboarding_email_challenges
            WHERE purpose = 'registration'
              AND normalized_email = $1
              AND {hash_column} = $2
              {legacy_filter}
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
        if email:
            await conn.execute(
                """
                UPDATE onboarding_email_challenges
                SET failed_attempts = LEAST(failed_attempts + 1, $2)
                WHERE id = (
                    SELECT id
                    FROM onboarding_email_challenges
                    WHERE purpose = 'registration'
                      AND normalized_email = $1
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

    email = challenge.get("normalized_email") or email
    if not email:
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
                VALUES (gen_random_uuid(), $1, $2, 'superuser', false)
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

    is_self_service_onboarding = identity.get("onboarding_state") is not None
    if identity["lifecycle_status"] == "pending" and all((
        challenge.get("business_name"),
        challenge.get("country_code"),
        challenge.get("base_currency_code"),
    )):
        financial = await update_onboarding_financial_profile(
            conn,
            identity["tenant_id"],
            OnboardingBusinessProfileUpdate(
                businessName=challenge["business_name"],
                country_code=challenge["country_code"],
                base_currency_code=challenge["base_currency_code"],
            ),
        )
        identity["tenant_name"] = financial.data.business_name
        identity["onboarding_state"] = financial.data.state
        identity["next_step"] = financial.data.next_step
        identity["lifecycle_status"] = "active"

    notification = None
    if is_self_service_onboarding:
        phone_number = challenge.get("phone_number")
        phone_country_code = challenge.get("phone_country_code")
        if phone_number and phone_country_code:
            await conn.execute(
                """
                UPDATE profile
                SET phone_number = $2, phone_country_code = $3, updated_at = NOW()
                WHERE id = $1
                """,
                identity["user_id"],
                phone_number,
                phone_country_code,
            )

        full_phone = (
            f"+{phone_country_code}{phone_number}"
            if phone_country_code and phone_number
            else None
        )
        lead = await conn.fetchrow(
            """
            INSERT INTO leads (
                profile_id, tenant_id, email, phone, source, status,
                utm_source, utm_campaign, utm_content
            )
            VALUES ($1, $2, $3, $4, 'self_service_registration', 'verified', $5, $6, $7)
            ON CONFLICT (tenant_id) WHERE source = 'self_service_registration'
            DO UPDATE SET
                email = EXCLUDED.email,
                phone = COALESCE(EXCLUDED.phone, leads.phone),
                status = 'verified'
            RETURNING id
            """,
            identity["user_id"],
            identity["tenant_id"],
            email,
            full_phone,
            challenge.get("first_source"),
            challenge.get("first_campaign"),
            challenge.get("first_content"),
        )
        metadata = json.dumps({
            "variant": challenge.get("last_variant"),
            "first_variant": challenge.get("first_variant"),
        })
        event = await conn.fetchrow(
            """
            INSERT INTO lead_interactions (
                lead_id, interaction_type, source, campaign, content,
                metadata, interaction_context
            )
            VALUES ($1, 'registration_verified', $2, $3, $4, $5::jsonb,
                    'self_service_registration')
            ON CONFLICT (lead_id, interaction_type)
                WHERE interaction_type = 'registration_verified'
            DO NOTHING
            RETURNING id
            """,
            lead["id"],
            challenge.get("last_source"),
            challenge.get("last_campaign"),
            challenge.get("last_content"),
            metadata,
        )
        if event:
            notification = {
                "email": email,
                "phone": phone_number,
                "phone_country_code": phone_country_code,
                "tenant_name": (
                    identity["tenant_name"]
                    if identity["tenant_name"] != "Negocio pendiente"
                    else None
                ),
                "status": identity["onboarding_state"],
                "source": challenge.get("last_source"),
                "content": challenge.get("last_content"),
                "campaign": challenge.get("last_campaign"),
                "variant": challenge.get("last_variant"),
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
    identity["registration_notification"] = notification
    return identity


def _require_tenant_id(tenant_id: Optional[UUID]) -> UUID:
    if not tenant_id:
        raise HTTPException(status_code=401, detail="Tenant ID is required")
    return tenant_id


def _profile_from_row(row: Any) -> Optional[TenantFinancialProfile]:
    if not row or not row.get("profile_tenant_id"):
        return None
    return TenantFinancialProfile(
        tenant_id=row["profile_tenant_id"],
        country_code=row["country_code"],
        base_currency_code=row["base_currency_code"],
        accounting_localization=row["accounting_localization"],
        document_mode=row["document_mode"],
        fiscal_provider=row["fiscal_provider"],
        selection_revision=row.get("selection_revision") or 1,
        created_at=row.get("profile_created_at"),
        updated_at=row.get("profile_updated_at"),
    )


def _ensure_pending_financial_state(row: Any) -> None:
    if not row:
        raise HTTPException(status_code=404, detail="Onboarding not found")
    is_pending = row["lifecycle_status"] == "pending"
    is_promoted = (
        row["lifecycle_status"] == "active"
        and row["state"] in {"starter_active", "payment_pending"}
    )
    if not is_pending and not is_promoted:
        raise HTTPException(
            status_code=409,
            detail={"code": "ONBOARDING_NOT_PENDING"},
        )
    if row["state"] not in PRE_PAYMENT_FINANCIAL_STATES:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ONBOARDING_FINANCIAL_PROFILE_LOCKED",
                "state": row["state"],
            },
        )


def _financial_response(row: Any) -> OnboardingFinancialResponse:
    state = row["state"]
    return OnboardingFinancialResponse(
        data=OnboardingFinancialData(
            businessName=row["business_name"],
            profile=_profile_from_row(row),
            catalog=financial_service._catalog(),
            currencies=financial_service._currencies(),
            state=state,
            nextStep=next_step_for_state(state),
        )
    )


async def _promote_onboarding_identity(conn, tenant_id: UUID) -> str:
    state_row = await conn.fetchrow(
        """
        UPDATE tenant_onboarding
        SET state = 'starter_active',
            updated_at = CASE
                WHEN state IS DISTINCT FROM 'starter_active' THEN NOW()
                ELSE updated_at
            END
        WHERE tenant_id = $1
          AND state IN ('business_profile_pending', 'terms_pending', 'starter_active')
        RETURNING state
        """,
        tenant_id,
    )
    if state_row is None:
        raise HTTPException(
            status_code=409,
            detail={"code": "ONBOARDING_ACTIVATION_CONFLICT"},
        )

    member_update = await conn.execute(
        """
        UPDATE tenant_members tm
        SET role = 'superuser', is_active = true
        FROM tenant_onboarding o
        WHERE o.tenant_id = $1
          AND tm.tenant_id = o.tenant_id
          AND tm.user_id = o.owner_user_id
          AND tm.role IN ('owner', 'admin', 'superuser')
        """,
        tenant_id,
    )
    tenant_update = await conn.execute(
        """
        UPDATE tenants
        SET lifecycle_status = 'active'
        WHERE id = $1 AND lifecycle_status IN ('pending', 'active')
        """,
        tenant_id,
    )
    if member_update != "UPDATE 1" or tenant_update != "UPDATE 1":
        raise HTTPException(
            status_code=409,
            detail={"code": "ONBOARDING_ACTIVATION_CONFLICT"},
        )
    return state_row["state"]


async def update_onboarding_financial_profile(
    conn,
    tenant_id: Optional[UUID],
    data: OnboardingBusinessProfileUpdate,
) -> OnboardingFinancialResponse:
    tenant_id = _require_tenant_id(tenant_id)
    context = await conn.fetchrow(
        """
        SELECT t.lifecycle_status, t.name AS business_name, o.state
        FROM tenants t
        JOIN tenant_onboarding o ON o.tenant_id = t.id
        WHERE t.id = $1
        FOR UPDATE OF t, o
        """,
        tenant_id,
    )
    _ensure_pending_financial_state(context)

    country_code, currency_code = financial_service.validate_country_currency_pair(
        data.country_code, data.base_currency_code
    )
    if context["state"] == "starter_active":
        profile = await conn.fetchrow(
            """
            SELECT tenant_id AS profile_tenant_id,
                   country_code, base_currency_code,
                   accounting_localization, document_mode, fiscal_provider,
                   selection_revision,
                   created_at AS profile_created_at,
                   updated_at AS profile_updated_at
            FROM tenant_financial_profiles
            WHERE tenant_id = $1
            FOR UPDATE
            """,
            tenant_id,
        )
        if (
            not profile
            or context["business_name"] != data.business_name
            or profile["country_code"] != country_code
            or profile["base_currency_code"] != currency_code
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ONBOARDING_FINANCIAL_PROFILE_LOCKED",
                    "state": context["state"],
                },
            )
        result = dict(profile)
        result["business_name"] = context["business_name"]
        result["lifecycle_status"] = "active"
        result["state"] = await _promote_onboarding_identity(conn, tenant_id)
        return _financial_response(result)

    await conn.execute(
        """
        UPDATE tenants
        SET name = $2
        WHERE id = $1
          AND name IS DISTINCT FROM $2
        """,
        tenant_id,
        data.business_name,
    )
    accounting_localization, document_mode, fiscal_provider = (
        financial_service._financial_mode(country_code)
    )
    profile = await conn.fetchrow(
        """
        INSERT INTO tenant_financial_profiles (
            tenant_id, country_code, base_currency_code,
            accounting_localization, document_mode, fiscal_provider,
            selection_revision
        )
        VALUES ($1, $2, $3, $4, $5, $6, 1)
        ON CONFLICT (tenant_id) DO UPDATE
        SET selection_revision = CASE
                WHEN tenant_financial_profiles.country_code IS DISTINCT FROM EXCLUDED.country_code
                  OR tenant_financial_profiles.base_currency_code IS DISTINCT FROM EXCLUDED.base_currency_code
                THEN tenant_financial_profiles.selection_revision + 1
                ELSE tenant_financial_profiles.selection_revision
            END,
            country_code = EXCLUDED.country_code,
            base_currency_code = EXCLUDED.base_currency_code,
            accounting_localization = EXCLUDED.accounting_localization,
            document_mode = EXCLUDED.document_mode,
            fiscal_provider = EXCLUDED.fiscal_provider,
            updated_at = CASE
                WHEN tenant_financial_profiles.country_code IS DISTINCT FROM EXCLUDED.country_code
                  OR tenant_financial_profiles.base_currency_code IS DISTINCT FROM EXCLUDED.base_currency_code
                THEN NOW()
                ELSE tenant_financial_profiles.updated_at
            END
        RETURNING tenant_id AS profile_tenant_id,
                  country_code, base_currency_code,
                  accounting_localization, document_mode, fiscal_provider,
                  selection_revision,
                  created_at AS profile_created_at,
                  updated_at AS profile_updated_at
        """,
        tenant_id,
        country_code,
        currency_code,
        accounting_localization,
        document_mode,
        fiscal_provider,
    )
    state = await _promote_onboarding_identity(conn, tenant_id)
    result = dict(profile)
    result["business_name"] = data.business_name
    result["lifecycle_status"] = "active"
    result["state"] = state
    return _financial_response(result)


async def accept_onboarding_terms(
    conn,
    session,
    *,
    client_ip: Optional[str],
    user_agent: Optional[str],
) -> dict[str, Any]:
    tenant_id = _require_tenant_id(session.tenant_id)
    context = await conn.fetchrow(
        """
        SELECT t.lifecycle_status, o.state, fp.country_code
        FROM tenants t
        JOIN tenant_onboarding o ON o.tenant_id = t.id
        LEFT JOIN tenant_financial_profiles fp ON fp.tenant_id = t.id
        WHERE t.id = $1
        FOR UPDATE OF t, o
        """,
        tenant_id,
    )
    if not context or context["lifecycle_status"] not in {"pending", "active"}:
        raise HTTPException(status_code=409, detail={"code": "ONBOARDING_NOT_PENDING"})
    if not context.get("country_code"):
        raise HTTPException(
            status_code=409,
            detail={"code": "ONBOARDING_FINANCIAL_PROFILE_REQUIRED"},
        )
    if context["state"] not in {"terms_pending", "payment_pending"}:
        raise HTTPException(
            status_code=409,
            detail={"code": "ONBOARDING_TERMS_NOT_AVAILABLE", "state": context["state"]},
        )

    result = await legal_service.accept_current_terms(
        conn,
        session,
        client_ip=client_ip,
        user_agent=user_agent,
        source="onboarding",
    )
    state_row = await conn.fetchrow(
        """
        UPDATE tenant_onboarding
        SET state = CASE
                WHEN state = 'terms_pending' THEN 'starter_active'
                ELSE state
            END,
            updated_at = CASE
                WHEN state = 'terms_pending' THEN NOW()
                ELSE updated_at
            END
        WHERE tenant_id = $1
        RETURNING state
        """,
        tenant_id,
    )
    result["data"]["onboarding"] = {
        "state": state_row["state"],
        "nextStep": next_step_for_state(state_row["state"]),
    }
    return result


async def ensure_onboarding_payment_ready(conn, session) -> None:
    """Fail closed unless a provisioned tenant completed profile and current terms."""
    tenant_id = _require_tenant_id(session.tenant_id)
    context = await conn.fetchrow(
        """
        SELECT t.lifecycle_status, o.state,
               fp.tenant_id AS profile_tenant_id
        FROM tenants t
        JOIN tenant_onboarding o ON o.tenant_id = t.id
        LEFT JOIN tenant_financial_profiles fp ON fp.tenant_id = t.id
        WHERE t.id = $1
        """,
        tenant_id,
    )
    if (
        not context
        or context["lifecycle_status"] not in {"pending", "active"}
        or context["state"] != "payment_pending"
        or not context.get("profile_tenant_id")
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "ONBOARDING_PAYMENT_NOT_READY"},
        )

    current = await legal_service.get_current_terms(conn, tenant_id)
    if not current:
        raise HTTPException(
            status_code=409,
            detail={"code": "ONBOARDING_CURRENT_TERMS_REQUIRED"},
        )
    acceptance = await legal_service.get_acceptance_for_version(
        conn, tenant_id, UUID(current["version_id"])
    )
    if not acceptance:
        raise HTTPException(
            status_code=409,
            detail={"code": "ONBOARDING_CURRENT_TERMS_REQUIRED"},
        )


async def activate_paid_onboarding_identity(conn, tenant_id: UUID) -> Optional[dict]:
    """Complete onboarding after payment, including already-provisioned identities."""
    context = await conn.fetchrow(
        """
        SELECT t.id AS tenant_id, t.name AS tenant_name, t.email AS tenant_email,
               t.lifecycle_status, o.id AS onboarding_id, o.state,
               o.owner_user_id, tm.id AS owner_member_id
        FROM tenants t
        JOIN tenant_onboarding o ON o.tenant_id = t.id
        JOIN tenant_members tm
          ON tm.tenant_id = t.id
         AND tm.user_id = o.owner_user_id
         AND tm.role IN ('owner', 'admin', 'superuser')
        WHERE t.id = $1
        FOR UPDATE OF t, o, tm
        """,
        tenant_id,
    )
    if context is None:
        return None

    if context["state"] != "payment_pending":
        return None

    owner_update = await conn.execute(
        """
        UPDATE tenant_members
        SET is_active = true, role = 'superuser'
        WHERE id = $1 AND role IN ('owner', 'admin', 'superuser')
        """,
        context["owner_member_id"],
    )
    onboarding_update = await conn.execute(
        """
        UPDATE tenant_onboarding
        SET state = 'active', updated_at = now()
        WHERE id = $1 AND state = 'payment_pending'
        """,
        context["onboarding_id"],
    )
    tenant_update = await conn.execute(
        """
        UPDATE tenants
        SET lifecycle_status = 'active'
        WHERE id = $1 AND lifecycle_status IN ('pending', 'active')
        """,
        tenant_id,
    )
    if owner_update != "UPDATE 1" or onboarding_update != "UPDATE 1" or tenant_update != "UPDATE 1":
        raise HTTPException(status_code=409, detail={"code": "ONBOARDING_ACTIVATION_CONFLICT"})
    return dict(context)


async def get_status_for_tenant(conn, tenant_id: UUID) -> OnboardingStatusResponse:
    tenant_id = _require_tenant_id(tenant_id)
    row = await conn.fetchrow(
        """
        SELECT t.id AS tenant_id, t.name AS business_name, t.lifecycle_status,
               o.state, o.email_verified_at,
               fp.tenant_id AS profile_tenant_id,
               fp.country_code, fp.base_currency_code,
               fp.accounting_localization, fp.document_mode, fp.fiscal_provider,
               fp.selection_revision,
               fp.created_at AS profile_created_at,
               fp.updated_at AS profile_updated_at
        FROM tenants t
        LEFT JOIN tenant_onboarding o ON o.tenant_id = t.id
        LEFT JOIN tenant_financial_profiles fp ON fp.tenant_id = t.id
        WHERE t.id = $1
        """,
        tenant_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Onboarding not found")
    current = await legal_service.get_current_terms(conn, tenant_id)
    acceptance = None
    if current:
        acceptance = await legal_service.get_acceptance_for_version(
            conn, tenant_id, UUID(current["version_id"])
        )
    return OnboardingStatusResponse(
        data=OnboardingStatus(
            tenantId=row["tenant_id"],
            lifecycleStatus=row["lifecycle_status"],
            businessName=row["business_name"],
            state=row.get("state"),
            nextStep=next_step_for_state(row.get("state")),
            emailVerifiedAt=row.get("email_verified_at"),
            financialProfile=_profile_from_row(row),
            termsAccepted=acceptance is not None,
            termsVersion=current["version"] if current else None,
        )
    )

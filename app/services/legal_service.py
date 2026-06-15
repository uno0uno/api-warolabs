import json
from datetime import datetime
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import HTTPException


TERMS_DOCUMENT_CODE = "terms_conditions"


def _json_value(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, str):
        return json.loads(value)
    return value


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if value else None


def _serialize_annex(row: Any) -> Dict[str, Any]:
    return {
        "id": str(row["id"]),
        "code": row["code"],
        "title": row["title"],
        "version": row["version"],
        "scope_type": row["scope_type"],
        "tenant_id": str(row["tenant_id"]) if row["tenant_id"] else None,
        "country": row["country"],
        "region": row["region"],
        "content_url": row["content_url"],
        "metadata": _json_value(row["metadata"], {}),
    }


def _serialize_version(row: Any, annexes: list) -> Dict[str, Any]:
    return {
        "document_id": str(row["document_id"]),
        "document_code": row["document_code"],
        "document_title": row["document_title"],
        "retention_years": row["retention_years"],
        "version_id": str(row["version_id"]),
        "version": row["version"],
        "effective_at": _iso(row["effective_at"]),
        "published_at": _iso(row["published_at"]),
        "content_url": row["content_url"],
        "content_sha256": row["content_sha256"],
        "metadata": _json_value(row["metadata"], {}),
        "annexes": annexes,
    }


def _serialize_acceptance(row: Any) -> Dict[str, Any]:
    return {
        "id": str(row["id"]),
        "tenant_id": str(row["tenant_id"]),
        "document_version_id": str(row["document_version_id"]),
        "user_id": str(row["user_id"]) if row["user_id"] else None,
        "source": row["source"],
        "accepted_at": _iso(row["accepted_at"]),
        "client_ip": row["client_ip"],
        "user_agent": row["user_agent"],
        "tenant_name": row["tenant_name_snapshot"],
        "legal_name": row["legal_name_snapshot"],
        "document_type": row["document_type_snapshot"],
        "document_number": row["document_number_snapshot"],
        "email": row["email_snapshot"],
        "actor_name": row["actor_name_snapshot"],
        "actor_email": row["actor_email_snapshot"],
        "document_code": row["document_code_snapshot"],
        "document_title": row["document_title_snapshot"],
        "version": row["version_snapshot"],
        "annexes": _json_value(row["annexes_snapshot"], []),
        "evidence": _json_value(row["evidence"], {}),
    }


def _require_tenant_id(tenant_id: Optional[UUID]) -> UUID:
    if not tenant_id:
        raise HTTPException(status_code=401, detail="Tenant ID is required")
    return tenant_id


async def get_current_terms(conn, tenant_id: Optional[UUID] = None) -> Optional[Dict[str, Any]]:
    row = await conn.fetchrow(
        """
        SELECT
            d.id AS document_id,
            d.code AS document_code,
            d.title AS document_title,
            d.retention_years,
            v.id AS version_id,
            v.version,
            v.effective_at,
            v.published_at,
            v.content_url,
            v.content_sha256,
            v.metadata
        FROM legal_documents d
        JOIN legal_document_versions v ON v.document_id = d.id
        WHERE d.code = $1
          AND v.status = 'published'
          AND v.effective_at <= now()
        ORDER BY v.effective_at DESC, v.created_at DESC
        LIMIT 1
        """,
        TERMS_DOCUMENT_CODE,
    )
    if not row:
        return None

    annex_rows = await conn.fetch(
        """
        SELECT id, code, title, version, scope_type, tenant_id, country, region,
               content_url, metadata
        FROM legal_document_annexes
        WHERE document_version_id = $1
          AND is_active = true
          AND (tenant_id IS NULL OR tenant_id = $2)
        ORDER BY sort_order ASC, code ASC
        """,
        row["version_id"],
        tenant_id,
    )
    return _serialize_version(row, [_serialize_annex(a) for a in annex_rows])


async def get_acceptance_for_version(conn, tenant_id: UUID, version_id: UUID) -> Optional[Dict[str, Any]]:
    row = await conn.fetchrow(
        """
        SELECT *
        FROM tenant_legal_acceptances
        WHERE tenant_id = $1 AND document_version_id = $2
        LIMIT 1
        """,
        tenant_id,
        version_id,
    )
    return _serialize_acceptance(row) if row else None


async def list_acceptance_audit_records(
    conn,
    tenant_id: UUID,
    *,
    document_version_id: Optional[UUID] = None,
    actor_email: Optional[str] = None,
    accepted_from: Optional[datetime] = None,
    accepted_to: Optional[datetime] = None,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    rows = await conn.fetch(
        """
        SELECT *
        FROM tenant_legal_acceptances
        WHERE tenant_id = $1
          AND ($2::uuid IS NULL OR document_version_id = $2)
          AND (
            $3::text IS NULL
            OR actor_email_snapshot ILIKE '%' || $3 || '%'
            OR email_snapshot ILIKE '%' || $3 || '%'
          )
          AND ($4::timestamptz IS NULL OR accepted_at >= $4)
          AND ($5::timestamptz IS NULL OR accepted_at <= $5)
        ORDER BY accepted_at DESC, created_at DESC
        LIMIT $6 OFFSET $7
        """,
        tenant_id,
        document_version_id,
        actor_email,
        accepted_from,
        accepted_to,
        limit,
        offset,
    )
    return {
        "success": True,
        "data": {
            "records": [_serialize_acceptance(row) for row in rows],
            "limit": limit,
            "offset": offset,
        },
    }


async def get_acceptance_audit_record(conn, tenant_id: UUID, acceptance_id: UUID) -> Dict[str, Any]:
    row = await conn.fetchrow(
        """
        SELECT *
        FROM tenant_legal_acceptances
        WHERE tenant_id = $1 AND id = $2
        LIMIT 1
        """,
        tenant_id,
        acceptance_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Legal acceptance evidence not found")
    return {"success": True, "data": _serialize_acceptance(row)}


async def get_terms_status(conn, tenant_id: Optional[UUID]) -> Dict[str, Any]:
    tenant_id = _require_tenant_id(tenant_id)
    current = await get_current_terms(conn, tenant_id)
    if not current:
        return {
            "success": True,
            "data": {
                "requires_acceptance": False,
                "current": None,
                "acceptance": None,
            },
        }

    acceptance = await get_acceptance_for_version(conn, tenant_id, UUID(current["version_id"]))
    return {
        "success": True,
        "data": {
            "requires_acceptance": acceptance is None,
            "current": current,
            "acceptance": acceptance,
        },
    }


async def has_current_terms_acceptance(conn, tenant_id: Optional[UUID]) -> bool:
    tenant_id = _require_tenant_id(tenant_id)
    current = await get_current_terms(conn, tenant_id)
    if not current:
        return True
    acceptance = await get_acceptance_for_version(conn, tenant_id, UUID(current["version_id"]))
    return acceptance is not None


async def ensure_current_terms_accepted(conn, tenant_id: Optional[UUID]) -> None:
    if await has_current_terms_acceptance(conn, tenant_id):
        return
    current = await get_current_terms(conn, tenant_id)
    raise HTTPException(
        status_code=409,
        detail={
            "code": "terms_acceptance_required",
            "message": "Debes aceptar los Terminos y Condiciones vigentes antes de continuar.",
            "document_version_id": current["version_id"] if current else None,
            "version": current["version"] if current else None,
        },
    )


async def _snapshot_tenant(conn, tenant_id: UUID, fallback_email: Optional[str]) -> Dict[str, Any]:
    row = await conn.fetchrow(
        """
        SELECT
            t.name AS tenant_name,
            t.email AS tenant_email,
            COALESCE(tfd.business_name, li.legal_name, t.name) AS legal_name,
            COALESCE(tfd.nit, li.nit) AS document_number,
            COALESCE(tfd.email, li.email, t.email, $2) AS email
        FROM tenants t
        LEFT JOIN tenant_fiscal_data tfd ON tfd.tenant_id = t.id
        LEFT JOIN legal_info li ON li.tenant_id = t.id
        WHERE t.id = $1
        LIMIT 1
        """,
        tenant_id,
        fallback_email,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return dict(row)


async def accept_current_terms(
    conn,
    session,
    *,
    client_ip: Optional[str],
    user_agent: Optional[str],
    source: str = "api",
) -> Dict[str, Any]:
    tenant_id = _require_tenant_id(session.tenant_id)
    current = await get_current_terms(conn, tenant_id)
    if not current:
        raise HTTPException(status_code=404, detail="No published terms document found")

    existing = await get_acceptance_for_version(conn, tenant_id, UUID(current["version_id"]))
    if existing:
        return {
            "success": True,
            "data": {
                "already_accepted": True,
                "current": current,
                "acceptance": existing,
            },
        }

    snapshot = await _snapshot_tenant(conn, tenant_id, session.email)
    annexes = current["annexes"]
    row = await conn.fetchrow(
        """
        INSERT INTO tenant_legal_acceptances (
            tenant_id,
            document_version_id,
            user_id,
            source,
            client_ip,
            user_agent,
            tenant_name_snapshot,
            legal_name_snapshot,
            document_type_snapshot,
            document_number_snapshot,
            email_snapshot,
            actor_name_snapshot,
            actor_email_snapshot,
            document_code_snapshot,
            document_title_snapshot,
            version_snapshot,
            annexes_snapshot,
            evidence
        )
        VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, 'NIT', $9, $10, $11, $12, $13, $14, $15,
            $16::jsonb,
            jsonb_build_object('retention_years', $17::int, 'server_time_source', 'database_now')
        )
        ON CONFLICT (tenant_id, document_version_id) DO NOTHING
        RETURNING *
        """,
        tenant_id,
        UUID(current["version_id"]),
        session.user_id,
        source,
        client_ip,
        user_agent,
        snapshot["tenant_name"],
        snapshot["legal_name"],
        snapshot["document_number"],
        snapshot["email"],
        session.name,
        session.email,
        current["document_code"],
        current["document_title"],
        current["version"],
        json.dumps(annexes),
        current["retention_years"],
    )
    if not row:
        acceptance = await get_acceptance_for_version(conn, tenant_id, UUID(current["version_id"]))
        already_accepted = True
    else:
        acceptance = _serialize_acceptance(row)
        already_accepted = False

    return {
        "success": True,
        "data": {
            "already_accepted": already_accepted,
            "current": current,
            "acceptance": acceptance,
        },
    }

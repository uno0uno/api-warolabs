"""Resolve tenant GL accounts by semantic role instead of localization codes."""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from uuid import UUID

from app.core.exceptions import APIError
from app.core.middleware import require_valid_session
from app.database import get_db_connection


class AccountRole:
    CASH = "CASH"
    BANK = "BANK"
    ACCOUNTS_RECEIVABLE = "ACCOUNTS_RECEIVABLE"
    INVENTORY = "INVENTORY"
    ACCOUNTS_PAYABLE = "ACCOUNTS_PAYABLE"
    SALES_REVENUE = "SALES_REVENUE"
    TAX_PAYABLE = "TAX_PAYABLE"
    COGS = "COGS"
    PAYROLL_EXPENSE = "PAYROLL_EXPENSE"
    CUSTOMER_ADVANCES = "CUSTOMER_ADVANCES"
    INC_PAYABLE = "INC_PAYABLE"
    IVA_PAYABLE = "IVA_PAYABLE"
    LIQUOR_TAX_PAYABLE = "LIQUOR_TAX_PAYABLE"
    CONTRACTOR_EXPENSE = "CONTRACTOR_EXPENSE"
    WITHHOLDING_PAYABLE = "WITHHOLDING_PAYABLE"
    EMPLOYEE_SS_PAYABLE = "EMPLOYEE_SS_PAYABLE"
    EMPLOYER_SS_PAYABLE = "EMPLOYER_SS_PAYABLE"
    EMPLOYER_SS_EXPENSE = "EMPLOYER_SS_EXPENSE"
    PRIMA_EXPENSE = "PRIMA_EXPENSE"
    PRIMA_PAYABLE = "PRIMA_PAYABLE"
    CESANTIAS_EXPENSE = "CESANTIAS_EXPENSE"
    CESANTIAS_PAYABLE = "CESANTIAS_PAYABLE"
    CESANTIAS_INTEREST_EXPENSE = "CESANTIAS_INTEREST_EXPENSE"
    CESANTIAS_INTEREST_PAYABLE = "CESANTIAS_INTEREST_PAYABLE"
    VACATION_EXPENSE = "VACATION_EXPENSE"
    VACATION_PAYABLE = "VACATION_PAYABLE"
    OVERTIME_EXPENSE = "OVERTIME_EXPENSE"
    DOTACION_EXPENSE = "DOTACION_EXPENSE"
    TERMINATION_EXPENSE = "TERMINATION_EXPENSE"
    BANK_FEES_EXPENSE = "BANK_FEES_EXPENSE"
    OTHER_INCOME = "OTHER_INCOME"


PAYMENT_ROLE_BY_SLUG = {
    "cash": AccountRole.CASH,
    "transfer": AccountRole.BANK,
    "check": AccountRole.BANK,
    "digital": AccountRole.BANK,
    "card": AccountRole.BANK,
    "credit_card": AccountRole.BANK,
    "debit_card": AccountRole.BANK,
    "other": AccountRole.BANK,
    "credit": AccountRole.ACCOUNTS_RECEIVABLE,
    "customer_wallet": AccountRole.CUSTOMER_ADVANCES,
    "table_session_advance": AccountRole.CUSTOMER_ADVANCES,
}

_TAX_BINDINGS = {
    "inc": ("inc_gl_account_id", "inc_gl_account_code", AccountRole.INC_PAYABLE),
    "iva": ("iva_gl_account_id", "iva_gl_account_code", AccountRole.IVA_PAYABLE),
    "liquor": (
        "liquor_tax_gl_account_id",
        "liquor_tax_gl_account_code",
        AccountRole.LIQUOR_TAX_PAYABLE,
    ),
}


@dataclass(frozen=True)
class AccountRef:
    id: UUID
    code: str
    name: str
    role: Optional[str]
    source: str


class MissingAccountRoleError(APIError):
    def __init__(self, tenant_id: UUID, role: str, source: Optional[str] = None):
        self.tenant_id = tenant_id
        self.role = role
        self.source = source
        suffix = " for {}".format(source) if source else ""
        super().__init__(
            "Required account role {} is not configured for tenant {}{}".format(
                role, tenant_id, suffix
            ),
            status_code=409,
            details={"code": "ACCOUNT_ROLE_MISSING", "role": role, "source": source},
        )


def _account_ref(row: Any, role: Optional[str], source: str) -> AccountRef:
    return AccountRef(
        id=row["id"],
        code=str(row["code"]),
        name=str(row["name"]),
        role=role,
        source=source,
    )


async def resolve_account(
    conn,
    tenant_id: UUID,
    role: str,
    required: bool = True,
    source: Optional[str] = None,
) -> Optional[AccountRef]:
    row = await conn.fetchrow(
        """
        SELECT
            COALESCE(override_account.id, default_account.id) AS id,
            COALESCE(override_account.code, default_account.code) AS code,
            COALESCE(override_account.name, default_account.name) AS name,
            CASE WHEN override_account.id IS NOT NULL THEN 'tenant_override'
                 ELSE 'localization_default' END AS binding_source
        FROM tenant_financial_profiles profile
        LEFT JOIN tenant_account_role_overrides binding
          ON binding.tenant_id = profile.tenant_id
         AND binding.role = $2
        LEFT JOIN tenant_accounts override_account
          ON override_account.id = binding.tenant_account_id
         AND override_account.tenant_id = profile.tenant_id
         AND override_account.is_active = TRUE
        LEFT JOIN account_template_role_defaults defaults
          ON defaults.localization_id = profile.accounting_localization
         AND defaults.role = $2
        LEFT JOIN tenant_accounts default_account
          ON default_account.tenant_id = profile.tenant_id
         AND default_account.template_id = defaults.account_template_id
         AND default_account.is_active = TRUE
        WHERE profile.tenant_id = $1
          AND COALESCE(override_account.id, default_account.id) IS NOT NULL
        """,
        tenant_id,
        role,
    )
    if row:
        return _account_ref(row, role, str(row["binding_source"]))
    if required:
        raise MissingAccountRoleError(tenant_id, role, source)
    return None


async def resolve_account_by_id(
    conn, tenant_id: UUID, account_id: Optional[UUID]
) -> Optional[AccountRef]:
    if not account_id:
        return None
    row = await conn.fetchrow(
        """
        SELECT id, code, name
        FROM tenant_accounts
        WHERE id = $1 AND tenant_id = $2 AND is_active = TRUE
        """,
        account_id,
        tenant_id,
    )
    return _account_ref(row, None, "explicit_account_id") if row else None


async def resolve_legacy_account(
    conn, tenant_id: UUID, code: Optional[str]
) -> Optional[AccountRef]:
    """Compatibility-only lookup for a stored tenant customization."""
    if not code:
        return None
    row = await conn.fetchrow(
        """
        SELECT id, code, name
        FROM tenant_accounts
        WHERE tenant_id = $1 AND code = $2 AND is_active = TRUE
        """,
        tenant_id,
        str(code),
    )
    return _account_ref(row, None, "legacy_binding") if row else None


async def resolve_configured_account(
    conn,
    tenant_id: UUID,
    account_id: Optional[UUID],
    legacy_code: Optional[str],
    fallback_role: str,
    source: Optional[str] = None,
) -> AccountRef:
    explicit = await resolve_account_by_id(conn, tenant_id, account_id)
    if explicit:
        return explicit
    legacy = await resolve_legacy_account(conn, tenant_id, legacy_code)
    if legacy:
        return legacy
    account = await resolve_account(conn, tenant_id, fallback_role, source=source)
    assert account is not None
    return account


def payment_role(payment_slug: Optional[str]) -> str:
    return PAYMENT_ROLE_BY_SLUG.get(payment_slug or "", AccountRole.CASH)


async def resolve_payment_account(
    conn,
    tenant_id: UUID,
    payment_slug: Optional[str],
    payment_method_id: Optional[UUID] = None,
    payment_group_id: Optional[UUID] = None,
    source: Optional[str] = None,
) -> AccountRef:
    resolved_slug = payment_slug
    if payment_method_id:
        row = await conn.fetchrow(
            """
            SELECT pm.gl_account_id AS method_account_id,
                   pm.gl_account_code AS method_legacy_code,
                   pmg.gl_account_id AS group_account_id,
                   pmg.gl_account_code AS group_legacy_code,
                   pmg.tenant_id AS group_tenant_id,
                   pmg.slug
            FROM payment_methods pm
            JOIN payment_method_groups pmg ON pmg.id = pm.group_id
            WHERE pm.id = $1 AND pm.tenant_id = $2 AND pm.is_active = TRUE
            """,
            payment_method_id,
            tenant_id,
        )
        if row:
            resolved_slug = row["slug"] or resolved_slug
            account = await resolve_account_by_id(
                conn, tenant_id, row["method_account_id"] or row["group_account_id"]
            )
            if account:
                return account
            legacy_code = row["method_legacy_code"]
            if not legacy_code and row["group_tenant_id"] is not None:
                legacy_code = row["group_legacy_code"]
            legacy = await resolve_legacy_account(conn, tenant_id, legacy_code)
            if legacy:
                return legacy

    if payment_group_id:
        row = await conn.fetchrow(
            """
            SELECT gl_account_id, gl_account_code, tenant_id, slug
            FROM payment_method_groups
            WHERE id = $1 AND (tenant_id = $2 OR tenant_id IS NULL) AND is_active = TRUE
            """,
            payment_group_id,
            tenant_id,
        )
        if row:
            resolved_slug = row["slug"] or resolved_slug
            account = await resolve_account_by_id(conn, tenant_id, row["gl_account_id"])
            if account:
                return account
            if row["tenant_id"] is not None:
                legacy = await resolve_legacy_account(
                    conn, tenant_id, row["gl_account_code"]
                )
                if legacy:
                    return legacy

    account = await resolve_account(
        conn,
        tenant_id,
        payment_role(resolved_slug),
        source=source or "payment_method",
    )
    assert account is not None
    return account


async def resolve_tax_account(
    conn,
    tenant_id: UUID,
    tax_config: Dict[str, Any],
    tax_kind: str,
    required: bool = True,
) -> Optional[AccountRef]:
    if tax_kind not in _TAX_BINDINGS:
        raise ValueError("Unsupported tax kind: {}".format(tax_kind))
    id_field, code_field, role = _TAX_BINDINGS[tax_kind]
    account_id = tax_config.get(id_field)
    if account_id and not isinstance(account_id, UUID):
        account_id = UUID(str(account_id))
    try:
        return await resolve_configured_account(
            conn,
            tenant_id,
            account_id,
            tax_config.get(code_field),
            role,
            source="{}_tax".format(tax_kind),
        )
    except MissingAccountRoleError:
        if required:
            raise
        return None


async def ensure_colombia_payroll(conn, tenant_id: UUID) -> None:
    enabled = await conn.fetchval(
        """
        SELECT country_code = 'CO'
           AND accounting_localization = 'WARO_CO_PUC_V1'
        FROM tenant_financial_profiles
        WHERE tenant_id = $1
        """,
        tenant_id,
    )
    if not enabled:
        raise APIError(
            "La nomina legal colombiana no esta disponible para este perfil financiero",
            status_code=409,
            details={"code": "COLOMBIA_PAYROLL_NOT_AVAILABLE"},
        )


async def require_colombia_payroll_capability(request) -> None:
    session = require_valid_session(request)
    if not session.tenant_id:
        raise APIError("Tenant ID is required", status_code=401)
    async with get_db_connection() as conn:
        await ensure_colombia_payroll(conn, session.tenant_id)


async def ensure_matias_dian(conn, tenant_id: UUID) -> None:
    """Fail closed when capabilities.matias_dian is off (non-CO profiles)."""
    enabled = await conn.fetchval(
        """
        SELECT country_code = 'CO'
        FROM tenant_financial_profiles
        WHERE tenant_id = $1
        """,
        tenant_id,
    )
    if not enabled:
        raise APIError(
            "La facturacion electronica Matias/DIAN no esta disponible para este perfil financiero",
            status_code=409,
            details={"code": "MATIAS_DIAN_NOT_AVAILABLE"},
        )


async def require_matias_dian_capability(request) -> None:
    session = require_valid_session(request)
    if not session.tenant_id:
        raise APIError("Tenant ID is required", status_code=401)
    async with get_db_connection() as conn:
        await ensure_matias_dian(conn, session.tenant_id)


async def list_role_bindings(conn, tenant_id: UUID) -> List[Dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT roles.role,
               roles.colombia_only,
               COALESCE(override_account.id, default_account.id) AS account_id,
               COALESCE(override_account.code, default_account.code) AS code,
               COALESCE(override_account.name, default_account.name) AS name,
               CASE WHEN override_account.id IS NOT NULL THEN 'tenant_override'
                    WHEN default_account.id IS NOT NULL THEN 'localization_default'
                    ELSE 'missing' END AS source
        FROM tenant_financial_profiles profile
        JOIN accounting_roles roles
          ON NOT roles.colombia_only OR profile.country_code = 'CO'
        LEFT JOIN tenant_account_role_overrides binding
          ON binding.tenant_id = profile.tenant_id AND binding.role = roles.role
        LEFT JOIN tenant_accounts override_account
          ON override_account.id = binding.tenant_account_id
         AND override_account.tenant_id = profile.tenant_id
         AND override_account.is_active = TRUE
        LEFT JOIN account_template_role_defaults defaults
          ON defaults.localization_id = profile.accounting_localization
         AND defaults.role = roles.role
        LEFT JOIN tenant_accounts default_account
          ON default_account.tenant_id = profile.tenant_id
         AND default_account.template_id = defaults.account_template_id
         AND default_account.is_active = TRUE
        WHERE profile.tenant_id = $1
        ORDER BY roles.role
        """,
        tenant_id,
    )
    return [dict(row) for row in rows]


async def set_role_override(
    conn, tenant_id: UUID, role: str, account_id: UUID
) -> None:
    valid_role = await conn.fetchval(
        "SELECT 1 FROM accounting_roles WHERE role = $1", role
    )
    account = await resolve_account_by_id(conn, tenant_id, account_id)
    if not valid_role or not account:
        raise APIError(
            "El rol o la cuenta contable no pertenece al tenant",
            status_code=400,
            details={"code": "ACCOUNT_ROLE_BINDING_INVALID"},
        )
    await conn.execute(
        """
        INSERT INTO tenant_account_role_overrides (tenant_id, role, tenant_account_id)
        VALUES ($1, $2, $3)
        ON CONFLICT (tenant_id, role) DO UPDATE SET
            tenant_account_id = EXCLUDED.tenant_account_id,
            updated_at = NOW()
        """,
        tenant_id,
        role,
        account_id,
    )


async def delete_role_override(conn, tenant_id: UUID, role: str) -> None:
    await conn.execute(
        "DELETE FROM tenant_account_role_overrides WHERE tenant_id = $1 AND role = $2",
        tenant_id,
        role,
    )

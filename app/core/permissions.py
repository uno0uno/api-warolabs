"""
Tenant-scoped, module-level permissions catalog and resolver.

Defines the canonical roles, modules, and default access matrix used by the
RBAC system, plus the cached `get_role_modules` resolver that merges
per-tenant overrides on top of the defaults. Epic 1 ships this as a pure
data + resolver layer — no router or middleware reads it yet (that wiring
lives in Epic 2 via `require_module()`).

Resolution semantics (`get_role_modules`):
  defaults  : `DEFAULT_ROLE_MODULES[role]` is the floor for that role.
  overrides : per-tenant rows in `tenant_role_module_overrides` MERGE on top
              of defaults — `granted=True` adds a module, `granted=False`
              removes one. Empty override set => defaults apply unchanged.
  cache     : `TTLCache(maxsize=1000, ttl=300)` keyed by (tenant_id, role).
              Override mutations MUST call `invalidate_role_modules` to keep
              the cache truthful — otherwise edits propagate after at most
              5 minutes of staleness, which is acceptable for shadow mode but
              not for enforcement.

Owner short-circuit: `Role.OWNER` always returns the full set of modules
without consulting overrides or the database — owners are super-admins by
construction and must not be lockable out of any module.

Customer is intentionally part of the enum: it tags public-portal users that
share the membership table but should never receive any staff module. Keeping
it in the enum lets the future safeguard trigger and `normalize_role` round-
trip every legacy row without raising.
"""
import logging
from enum import Enum
from typing import Awaitable, Callable, Dict, FrozenSet, Optional, Union
from uuid import UUID

from cachetools import TTLCache
from fastapi import HTTPException, Request, status

from app.database import get_db_connection


class Role(str, Enum):
    """Canonical staff + customer roles."""
    OWNER = "owner"
    ADMIN = "admin"
    SUPERVISOR = "supervisor"
    CASHIER = "cashier"
    KITCHEN = "kitchen"
    CUSTOMER = "customer"


class Module(str, Enum):
    """Functional modules the RBAC layer can gate.

    Grouped by business area as defined in Epic 2 (#164). 14 modules in
    Spanish to match the language operators use to talk about their own
    business. Each module corresponds to a router group that Epic 2 will
    decorate with `require_module()`.
    """
    POS = "pos"                          # pos_cart, tables, comandas, online_orders
    VENTAS = "ventas"                    # orders, online_cart
    DESPACHO = "despacho"                # placeholder — no routers yet (admin_orders deleted in #187)
    MENU = "menu"                        # menu, products, modifiers, combos, categories, recipe_bases, ingredients
    OPERACIONES = "operaciones"          # tenant_config, stations
    ABASTECIMIENTO = "abastecimiento"    # purchases, suppliers, inventory, admin_ingredients, ingredient_purchase_units
    ANALITICA = "analitica"              # analytics, articles
    FINANZAS = "finanzas"                # accounting, expenses, salaries, cierre, cartera, credit, payment_methods, financial
    FACTURACION = "facturacion"          # facturacion, invoices, support_documents
    EQUIPO = "equipo"                    # tenants, invitations
    INTEGRACIONES = "integraciones"      # api_tokens, webhooks, public_api
    MI_PLAN = "mi_plan"                  # billing
    MI_NEGOCIO = "mi_negocio"            # tenant_config (shared)


_ALL_MODULES: FrozenSet[Module] = frozenset(Module)


# Permissive defaults that mirror today's effective access. Tightening happens
# per-tenant via overrides + by flipping `tenants.permissions_enforcement_mode`
# from 'disabled' to 'shadow'/'enforce' once a tenant has reviewed the matrix.
DEFAULT_ROLE_MODULES: Dict[Role, FrozenSet[Module]] = {
    Role.OWNER: _ALL_MODULES,
    Role.ADMIN: frozenset({
        Module.POS,
        Module.VENTAS,
        Module.DESPACHO,
        Module.MENU,
        Module.OPERACIONES,
        Module.ABASTECIMIENTO,
        Module.ANALITICA,
        Module.FINANZAS,
        Module.FACTURACION,
        Module.INTEGRACIONES,
        Module.MI_PLAN,
        # No EQUIPO — admin manages operations, not membership/role changes
        # No MI_NEGOCIO — business identity / fiscal / DIAN is owner-only (E2.7/E2.15)
        # No EVENTOS — Eventos lives in warotickets.com (external product), removed in #212
    }),
    Role.SUPERVISOR: frozenset({
        Module.POS,
        Module.VENTAS,
        Module.DESPACHO,
        Module.MENU,
        Module.OPERACIONES,
        Module.ABASTECIMIENTO,
        Module.ANALITICA,
        # No MI_NEGOCIO — owner-only
    }),
    Role.CASHIER: frozenset({
        Module.POS,
        Module.VENTAS,
        Module.MENU,  # Read access for POS catalog (products + categories)
    }),
    Role.KITCHEN: frozenset({
        Module.DESPACHO,
    }),
    Role.CUSTOMER: frozenset(),
}


_LEGACY_ROLE_MAP: Dict[str, Role] = {
    "superuser": Role.OWNER,
    "employee": Role.CASHIER,
    "member": Role.CASHIER,
    "promotor": Role.CASHIER,
}


def normalize_role(s: str) -> Role:
    """Translate legacy + canonical role strings to a `Role` enum value.

    Legacy mappings (stable for the transition; flipped permanently in Epic 6):
        superuser -> OWNER
        employee  -> CASHIER
        member    -> CASHIER

    Canonical strings (`owner`, `admin`, `supervisor`, `cashier`, `kitchen`,
    `customer`) round-trip unchanged. Unknown values raise ValueError so the
    caller can decide on a fallback (e.g. log + reject vs. log + treat as
    customer).
    """
    if not isinstance(s, str):
        raise ValueError(f"role must be a string, got {type(s).__name__}")
    key = s.strip().lower()
    if key in _LEGACY_ROLE_MAP:
        return _LEGACY_ROLE_MAP[key]
    try:
        return Role(key)
    except ValueError as exc:
        raise ValueError(f"unknown role: {s!r}") from exc


# Process-local cache. NOTE: not shared across workers/replicas — an override
# edit on one worker becomes visible on other workers within `ttl` seconds at
# most. Acceptable while enforcement_mode is 'disabled' or 'shadow'; revisit
# (e.g. swap for Redis-backed cache) before turning enforcement on widely.
_role_modules_cache: TTLCache = TTLCache(maxsize=1000, ttl=300)


async def get_role_modules(
    tenant_id: UUID,
    role: Union[Role, str],
) -> FrozenSet[Module]:
    """Resolve the effective module set for `(tenant_id, role)`.

    Algorithm:
      1. Normalize `role` (accepts Role enum or any legacy/canonical string).
      2. Owner short-circuit: return all modules without touching DB.
      3. Cache lookup: hit returns immediately.
      4. Cache miss: read overrides, merge on top of defaults, store, return.

    The function is async because the override read goes through the asyncpg
    pool. Owner calls and cache hits never await on I/O.
    """
    normalized = role if isinstance(role, Role) else normalize_role(role)

    if normalized is Role.OWNER:
        return DEFAULT_ROLE_MODULES[Role.OWNER]

    cache_key = (tenant_id, normalized)
    cached = _role_modules_cache.get(cache_key)
    if cached is not None:
        return cached

    defaults = DEFAULT_ROLE_MODULES[normalized]
    async with get_db_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT module, granted
              FROM tenant_role_module_overrides
             WHERE tenant_id = $1 AND role = $2
            """,
            tenant_id,
            normalized.value,
        )

    if not rows:
        _role_modules_cache[cache_key] = defaults
        return defaults

    effective = set(defaults)
    for row in rows:
        module_str = row["module"]
        try:
            module = Module(module_str)
        except ValueError:
            # Unknown module string in DB — ignore (fail open) so a typo in an
            # override row does not lock anyone out. Logged at the admin layer.
            continue
        if row["granted"]:
            effective.add(module)
        else:
            effective.discard(module)

    result = frozenset(effective)
    _role_modules_cache[cache_key] = result
    return result


def invalidate_role_modules(
    tenant_id: UUID,
    role: Optional[Union[Role, str]] = None,
) -> None:
    """Drop cached entries for `(tenant_id, role)` or all roles of a tenant.

    Call after every successful insert/update/delete on
    `tenant_role_module_overrides`. Cheap (process-local dict eviction) so the
    admin endpoint should call it unconditionally on success.
    """
    if role is None:
        keys_to_drop = [k for k in _role_modules_cache if k[0] == tenant_id]
        for key in keys_to_drop:
            _role_modules_cache.pop(key, None)
        return

    normalized = role if isinstance(role, Role) else normalize_role(role)
    _role_modules_cache.pop((tenant_id, normalized), None)


# ─── Epic 2 (#164): require_module dependency + enforcement-mode cache ─────

# Tenant enforcement mode cache. Shorter TTL than the role-modules cache
# because flipping a tenant from `shadow` to `enforce` should propagate
# fast — operators expect a manual rollout step to take effect within a
# minute, not five.
_enforcement_mode_cache: TTLCache = TTLCache(maxsize=1000, ttl=60)

# Dedicated logger so ops can route shadow events to a specific sink
# (Discord channel, Loki stream, etc.) without polluting the request log.
_shadow_logger = logging.getLogger("permissions.shadow")


VALID_MODES = frozenset({"disabled", "shadow", "enforce"})


async def get_enforcement_mode(tenant_id: UUID) -> str:
    """Resolve `tenants.permissions_enforcement_mode` for a tenant.

    Cached for 60s. Defaults to `'disabled'` if the row is missing or the
    value is unknown — fail-open is safe here because `disabled` mirrors
    today's behavior (no gating).
    """
    cached = _enforcement_mode_cache.get(tenant_id)
    if cached is not None:
        return cached

    async with get_db_connection() as conn:
        mode = await conn.fetchval(
            "SELECT permissions_enforcement_mode FROM tenants WHERE id = $1",
            tenant_id,
        )

    if mode not in VALID_MODES:
        mode = "disabled"

    _enforcement_mode_cache[tenant_id] = mode
    return mode


def invalidate_enforcement_mode(tenant_id: UUID) -> None:
    """Drop the cached enforcement mode for `tenant_id`.

    Call this after every successful UPDATE on
    `tenants.permissions_enforcement_mode` so a flip from `shadow` to
    `enforce` (or back) takes effect on the next request instead of after
    the 60s TTL.
    """
    _enforcement_mode_cache.pop(tenant_id, None)


def _shadow_or_deny(
    mode: str,
    tenant_id: UUID,
    user_id: Optional[UUID],
    role: Optional[str],
    module: Module,
    reason: str,
    path: str,
) -> None:
    """Branch on enforcement mode: log+allow under shadow, raise 403 under enforce.

    `disabled` never reaches this function (the dependency returns earlier),
    so we only handle `shadow` and `enforce` here.
    """
    if mode == "shadow":
        # Wrap fields under a single key to avoid colliding with reserved
        # LogRecord attributes (`module`, `pathname`, etc.). Handlers can
        # pull `record.permission_event` to ship the structured payload.
        _shadow_logger.warning(
            "would_deny",
            extra={
                "permission_event": {
                    "tenant_id": str(tenant_id) if tenant_id else None,
                    "user_id": str(user_id) if user_id else None,
                    "role": role,
                    "module": module.value,
                    "reason": reason,
                    "path": path,
                }
            },
        )
        return
    # enforce
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Sin permiso para el módulo {module.value}",
    )


def require_module(module: Module) -> Callable[[Request], Awaitable[None]]:
    """Build a FastAPI dependency that gates `module` per tenant mode.

    Reads `tenants.permissions_enforcement_mode`:
      * `disabled` → bypass entirely (current behavior, no DB hit beyond
        the cached lookup).
      * `shadow`   → if the user lacks the module, log a `permissions.shadow`
        event and allow the request through. Used to surface false positives
        before flipping to enforce.
      * `enforce`  → if the user lacks the module, raise 403.

    Owner short-circuit happens inside `get_role_modules`. Sessions without
    a valid role (no membership row, KDS tokens, API keys without role
    plumbing) are treated as "no staff modules" — denied or shadow-logged.
    Sessions that aren't valid at all return early so `require_valid_session`
    (still called inside handlers) can raise 401 with its own message —
    keeps responsibilities split.

    Usage:
        from fastapi import Depends
        from app.core.permissions import Module, require_module

        @router.get("/billing", dependencies=[Depends(require_module(Module.MI_PLAN))])
        async def list_billing(...):
            ...
    """
    async def dependency(request: Request) -> None:
        from app.core.middleware import get_session_context  # local import to avoid cycle

        session = get_session_context(request)
        if not session.is_valid:
            return  # require_valid_session in the handler will surface 401

        tenant_id = session.tenant_id
        if not tenant_id:
            return  # no tenant resolved → cannot gate; let handler decide

        mode = await get_enforcement_mode(tenant_id)
        if mode == "disabled":
            return

        path = request.url.path
        raw_role = session.role
        if not raw_role:
            _shadow_or_deny(
                mode, tenant_id, session.user_id, None, module,
                reason="no-membership", path=path,
            )
            return

        try:
            normalized = normalize_role(raw_role)
        except ValueError:
            _shadow_or_deny(
                mode, tenant_id, session.user_id, raw_role, module,
                reason="unknown-role", path=path,
            )
            return

        allowed = await get_role_modules(tenant_id, normalized)
        if module in allowed:
            return

        _shadow_or_deny(
            mode, tenant_id, session.user_id, normalized.value, module,
            reason="not-in-matrix", path=path,
        )

    return dependency


# ─────────────────────────────────────────────────────────────────────
# Auto-handoff predicate for the waiter attribution family
# (warocol.com#574 — POS session override; warocol.com#575 — order
# attribution).
# ─────────────────────────────────────────────────────────────────────
async def can_reassign_waiter(
    caller_user_id: Optional[UUID],
    caller_role: Optional[str],
    tenant_id: UUID,
    current_waiter_member_id: Optional[UUID],
) -> bool:
    """Auto-handoff predicate.

    Returns True if the caller is allowed to set/change a waiter
    assignment given the current state. Caller is allowed when:

      1. No one currently has the lock (`current_waiter_member_id` is
         None — covers fresh sessions AND legacy rows with no opener).
      2. Caller is supervisor or higher (OWNER/ADMIN/SUPERVISOR by
         normalized role).
      3. Caller IS the current waiter (handoff to another or release).

    Returns False otherwise — typically a cashier trying to take a
    table that already belongs to a different cashier.

    Used by:
      - `table_assignments_service.set_session_waiter` (#574)
      - `table_assignments_service.set_order_served_by` (#575, future)
    """
    if current_waiter_member_id is None:
        return True

    if caller_role:
        try:
            normalized = normalize_role(caller_role)
            if normalized in (Role.OWNER, Role.ADMIN, Role.SUPERVISOR):
                return True
        except ValueError:
            pass

    if caller_user_id is None:
        return False

    # Look up caller's tenant_member id and compare with the current waiter.
    async with get_db_connection(use_transaction=False) as conn:
        caller_member_id = await conn.fetchval(
            """
            SELECT id FROM tenant_members
            WHERE user_id = $1 AND tenant_id = $2 AND is_active = true
            """,
            caller_user_id,
            tenant_id,
        )
    return caller_member_id == current_waiter_member_id

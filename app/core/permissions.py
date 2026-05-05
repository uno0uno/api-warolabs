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
from enum import Enum
from typing import Dict, FrozenSet, Optional, Union
from uuid import UUID

from cachetools import TTLCache

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
    DESPACHO = "despacho"                # admin_orders
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
    EVENTOS = "eventos"                  # TBD if/when routers exist


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
        Module.MI_NEGOCIO,
        Module.EVENTOS,
        # No EQUIPO — admin manages operations, not membership/role changes
    }),
    Role.SUPERVISOR: frozenset({
        Module.POS,
        Module.VENTAS,
        Module.DESPACHO,
        Module.MENU,
        Module.OPERACIONES,
        Module.ABASTECIMIENTO,
        Module.ANALITICA,
        Module.MI_NEGOCIO,
    }),
    Role.CASHIER: frozenset({
        Module.POS,
        Module.VENTAS,
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

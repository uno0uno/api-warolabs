"""
Tenant-scoped, module-level permissions catalog.

Defines the canonical roles, modules, and default access matrix used by the
RBAC system. Epic 1 ships this catalog as a pure data layer — no router or
middleware reads it yet (that wiring lives in Epic 2 via `require_module()`).

Resolution semantics (used by `get_role_modules` once #E1.4 lands):
  defaults  : `DEFAULT_ROLE_MODULES[role]` is the floor for that role.
  overrides : per-tenant rows in `tenant_role_module_overrides` MERGE on top
              of defaults — `granted=True` adds a module, `granted=False`
              removes one. Empty override set => defaults apply unchanged.

Owner short-circuit: `Role.OWNER` always returns the full set of modules
without consulting overrides or the database — owners are super-admins by
construction and must not be lockable out of any module.

Customer is intentionally part of the enum: it tags public-portal users that
share the membership table but should never receive any staff module. Keeping
it in the enum lets the future safeguard trigger and `normalize_role` round-
trip every legacy row without raising.
"""
from enum import Enum
from typing import Dict, FrozenSet


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

    Grouped by concern: tenant administration, catalog, operations, finance.
    Total: 17 modules.
    """
    # Tenant administration
    SETTINGS = "settings"
    BILLING = "billing"
    MEMBERS = "members"
    INVITATIONS = "invitations"
    API_TOKENS = "api_tokens"
    # Catalog
    MENU = "menu"
    CATEGORIES = "categories"
    INVENTORY = "inventory"
    PURCHASES = "purchases"
    SUPPLIERS = "suppliers"
    # Operations
    POS = "pos"
    TABLES = "tables"
    KDS = "kds"
    ORDERS = "orders"
    # Finance & analytics
    ACCOUNTING = "accounting"
    SALARIES = "salaries"
    ANALYTICS = "analytics"


_ALL_MODULES: FrozenSet[Module] = frozenset(Module)


# Permissive defaults that mirror today's effective access. Tightening happens
# per-tenant via overrides + by flipping `tenants.permissions_enforcement_mode`
# from 'disabled' to 'shadow'/'enforce' once a tenant has reviewed the matrix.
DEFAULT_ROLE_MODULES: Dict[Role, FrozenSet[Module]] = {
    Role.OWNER: _ALL_MODULES,
    Role.ADMIN: frozenset({
        Module.SETTINGS,
        Module.BILLING,
        Module.INVITATIONS,
        Module.API_TOKENS,
        Module.MENU,
        Module.CATEGORIES,
        Module.INVENTORY,
        Module.PURCHASES,
        Module.SUPPLIERS,
        Module.POS,
        Module.TABLES,
        Module.KDS,
        Module.ORDERS,
        Module.ACCOUNTING,
        Module.SALARIES,
        Module.ANALYTICS,
    }),
    Role.SUPERVISOR: frozenset({
        Module.MENU,
        Module.CATEGORIES,
        Module.INVENTORY,
        Module.PURCHASES,
        Module.SUPPLIERS,
        Module.POS,
        Module.TABLES,
        Module.KDS,
        Module.ORDERS,
        Module.ANALYTICS,
    }),
    Role.CASHIER: frozenset({
        Module.POS,
        Module.TABLES,
        Module.ORDERS,
    }),
    Role.KITCHEN: frozenset({
        Module.KDS,
        Module.ORDERS,
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

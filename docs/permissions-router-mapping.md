# Permissions: Router → Module Mapping

**Status:** Source of truth for Epic 2 (#164) wiring sub-tasks (E2.3 → E2.16).
**Origin:** [#186 audit](https://github.com/uno0uno/api-warolabs/issues/186).
**Last updated:** 2026-05-09 (initial commit, post #185 / E2.14 done).

This document maps each FastAPI router under `app/routers/` to the `Module`
enum value it should be gated under via `Depends(require_module(Module.X))`,
or marks it as `public` (no gating) / `skip` (excluded for a specific reason)
/ `mixed` (split across two modules at the endpoint level).

The factory `require_module()` and the `Module` enum live in
[`app/core/permissions.py`](../app/core/permissions.py). The first router
wired against this catalog was `billing.py` in #185 (E2.14, MI_PLAN).

> **Update protocol.** Any PR that adds, removes or renames a file under
> `app/routers/` MUST update this table in the same PR. The Epic 2 rollout
> phases (`shadow` → `enforce`) depend on this doc being current.

---

## Authoritative Table — 51 routers

| router_file | mount_prefix | endpoints | module | auth_today | notes |
|---|---|---|---|---|---|
| `accounting.py` | `/accounting` | 13 | **FINANZAS** | session | Chart of accounts CRUD, balance, P&L |
| `address_profile.py` | `/online/addresses` | 6 | **public** | none | Direcciones de delivery (clientes online) |
| `admin_ingredients.py` | `/admin/ingredients` | 6 | **ABASTECIMIENTO** | session | Catálogo global de ingredientes |
| `analytics.py` | `/analytics` | 6 | **ANALITICA** | session | Dashboard analítico, alertas |
| `api_tokens.py` | `/api-tokens` | 6 | **INTEGRACIONES** | session | API token CRUD + scopes |
| `articles.py` | `/blog` | 3 | **public** | none | Blog público (lista + detalle) |
| `auth.py` | `/auth` | 7 | **skip** | none | Login/sesión — corre SIN sesión |
| `billing.py` | `/billing` | 10 | **MI_PLAN** | mixed | DONE en #185. Webhook + cron skip con `# NOTE:` |
| `cartera.py` | `/cartera` | 4 | **FINANZAS** | session | Cartera (cuentas por cobrar) |
| `categories.py` | `/menu/categories` | 2 | **MENU** | session | Listado con filtro global+tenant |
| `cierre.py` | `/cierre` | 9 | **FINANZAS** | session | Cierre X/Z diario |
| `comandas.py` | `/api/comandas` | 11 | **POS** | session | KDS lifecycle. ⚠️ KDS-public paths via `?token=` middleware quedan sin gate (ver §1) |
| `combos.py` | `/menu/combos` | 6 | **MENU** | session | Combo CRUD |
| `credit.py` | `/credit` | 3 | **FINANZAS** | session | Pagos a crédito |
| `customer_portal.py` | `/customer` | 7 | **public** | none | Portal del cliente final (JWT customer, no sesión de operador) |
| `customers.py` | `/customers` | 6 | **VENTAS** | session | Búsqueda + perfil de clientes (operador) |
| `documents.py` | `/api/documents` | 4 | **FACTURACION** | session | Documentos electrónicos (lista, PDF/XML) |
| `expenses.py` | `/finance/expenses` | 14 | **FINANZAS** | session | CRUD de gastos + categorías |
| `facturacion.py` | `/api/acquirer + /api/facturacion + /api/payroll` | 3 | **FACTURACION** | session | 3 sub-routers (acquirer, catalog, payroll) — todos FACTURACION |
| `financial.py` | (sin prefix) | 3 | **FINANZAS** | session | TIR, rentabilidad de productos |
| `ingredient_purchase_units.py` | `/suppliers/ingredient-purchase-units` | 6 | **ABASTECIMIENTO** | session | Unidades de compra |
| `ingredients.py` | `/suppliers/ingredients` | 10 | **ABASTECIMIENTO** | session | Custom ingredients + catálogo |
| `inventory.py` | `/inventory` | 4 | **ABASTECIMIENTO** | session | Stock + ajustes |
| `invitations.py` | `/invitations` | 4 | **EQUIPO** | session | ⚠️ `/invitations/accept` es token-público (ver §2) |
| `invoices.py` | `/api/invoices` | 4 | **FACTURACION** | session | Notas crédito/débito, RADIAN |
| `leads.py` | `/leads` | 2 | **public** | none | Captura de leads (homepage) |
| `menu.py` | `/menu` | 1 | **MENU** | session | Name-check genérico |
| `modifiers.py` | `/menu/modifier-groups` | 6 | **MENU** | session | Grupos de modificadores |
| `notifications.py` | `/notifications` | 4 | **POS** | session | Notificaciones operador (SSE) — ver ambigüedad en §7 |
| `online_cart.py` | `/online/cart` | 10 | **public** | none | Carrito online (anónimo, session_id) |
| `online_orders.py` | `/online/orders` | 4 | **VENTAS** | session | Operador gestiona pedidos online |
| `online_verification.py` | `/online/otp` | 3 | **public** | none | OTP por email del cliente |
| `orders.py` | `/orders` | 18 | **VENTAS** | session | Dashboard de ventas, métricas |
| `payment_methods.py` | `/finanzas/metodos-pago + /pos/payment-methods` | 10 | **mixed** | session | Split: finanzas_router (FINANZAS) + pos_router (POS read-only) — ver §3 |
| `pos_cart.py` | `/pos/cart` | 11 | **POS** | session | Carrito POS, checkout |
| `products.py` | `/menu/products` | 7 | **MENU** | session | Producto CRUD + receta + imagen |
| `public_api.py` | `/v1` | 25 | **INTEGRACIONES** | api_key | API pública con API key (clientes externos) |
| `public_restaurant.py` | `/public/restaurant` | 4 | **public** | none | Lista + detalle por slug |
| `purchases.py` | `/suppliers/purchases` | 26 | **ABASTECIMIENTO** | session | Compras + estados + factura |
| `recipe_bases.py` | `/menu/recipe-bases` | 5 | **MENU** | session | Templates de receta |
| `salaries.py` | `/salaries` | 29 | **FINANZAS** | session | Nómina + prima + cesantías + PILA |
| `stations.py` | `/api/stations` | 15 | **OPERACIONES** | session | Estaciones de cocina + routing. ⚠️ KDS-token endpoints sin gate (ver §1) |
| `supplier_portal.py` | `/supplier-portal` | 8 | **public** | token | Portal del proveedor (token, no sesión) |
| `suppliers.py` | `/suppliers/providers` | 10 | **ABASTECIMIENTO** | session | Proveedor CRUD |
| `support_documents.py` | `/api/support-documents` | 2 | **FACTURACION** | session | DIAN documento soporte |
| `tables.py` | `/tables` | 19 | **POS** | session | Mesas + tab + sesión de mesa |
| `tenant_config.py` | `/api/tenant` | 15 | **mixed** | session | Split obligatorio: OPERACIONES + MI_NEGOCIO — ver §4 |
| `tenants.py` | `/tenants` | 5 | **EQUIPO** | session | Tenant create + member CRUD |
| `v1_ordering.py` | `/v1/cart + /v1/addresses + /v1/otp + /v1/customer + /v1/product` | 7 | **INTEGRACIONES** | api_key | V1 ordering API (clientes externos) |
| `waros.py` | `/admin/waros` | 8 | **POS** | session | Sistema de loyalty (puntos WaRo) |
| `webhooks.py` | `/api/webhooks` | 1 | **skip** | signature | Bridge a api-facturacion (signature-verified, no sesión) |

---

## Coverage Summary

Total: **51 routers**, **~394 endpoints** (was 52/395 before #187 deleted `admin_orders.py`).

| Module | Routers | Sub-task | Status |
|---|---|---|---|
| **POS** | comandas, notifications, pos_cart, tables, waros + payment_methods/pos | 5+1 | ✅ E2.3 (#188) — DONE (51 endpoints gated, 3 KDS-direct excluded) |
| **VENTAS** | customers, online_orders, orders | 3 | ✅ E2.4 (#189) — DONE (28 endpoints gated, no exclusions) |
| **DESPACHO** | (no routers — placeholder, like EVENTOS) | 0 | ✅ E2.5 (#187) — DONE (deleted dead `admin_orders.py`) |
| **MENU** | categories, combos, menu, modifiers, products, recipe_bases | 6 | E2.6 (#190) — pending |
| **OPERACIONES** | stations + tenant_config (operaciones part) | 1+1 | E2.7 (#191) — pending |
| **ABASTECIMIENTO** | admin_ingredients, ingredient_purchase_units, ingredients, inventory, purchases, suppliers | 6 | E2.8 (#195) — pending |
| **ANALITICA** | analytics | 1 | E2.9 (#193) — pending |
| **FINANZAS** | accounting, cartera, cierre, credit, expenses, financial, salaries + payment_methods/finanzas | 7+1 | E2.10 (#198) — pending |
| **FACTURACION** | documents, facturacion (3 sub-routers), invoices, support_documents | 4 | E2.11 (#194) — pending |
| **EQUIPO** | invitations (excl. /accept), tenants | 2 | E2.12 (#196) — pending |
| **INTEGRACIONES** | api_tokens, public_api, v1_ordering | 3 | E2.13 (#197) — pending |
| **MI_PLAN** | billing | 1 | ✅ E2.14 (#185, PR #202) — DONE |
| **MI_NEGOCIO** | tenant_config (mi_negocio part) | 1 | E2.15 (#199) — pending |
| **EVENTOS** | (no routers exist) | 0 | E2.16 (#200) — no-op |
| **public** | address_profile, articles, customer_portal, leads, online_cart, online_verification, public_restaurant, supplier_portal | 8 | n/a — never gated |
| **skip** | auth, webhooks | 2 | n/a — explicit exclusion |
| **mixed (split-by-endpoint)** | payment_methods, tenant_config | 2 | split across two sub-tasks |

---

## §1. KDS public paths in `comandas.py` and `stations.py`

The KDS (kitchen display screens) is unauthenticated hardware that consumes
some endpoints under `/api/comandas` and `/api/stations` via the `?token=`
query param. The middleware (`app/core/middleware.py`) has a `kds_public`
block that:

1. Resolves the token against `kds_tokens` and injects a synthetic
   `SessionContext` with the correct `tenant_id` (so `require_valid_session`
   succeeds inside the handler).
2. Carries `role=None` on that session.

When `require_module()` is wired on these routers (E2.3 + E2.7), specific
KDS-consumed endpoints must NOT receive the dependency, because the synthetic
session has no role and would always be denied. Per-endpoint exclusion is
mandatory — DO NOT use router-level `dependencies=[]`.

**Confirmed exclusions in `comandas.py`** (post-E2.3, see #188):

| Endpoint | Frontend caller |
|---|---|
| `GET /api/comandas` | `pages/cocina/[id].vue:53` (with `?station_id=&token=`) |
| `PATCH /api/comandas/{id}/status` | `components/cocina/ComandaCard.vue:72` (with `?token=`) |
| `PATCH /api/comandas/{id}/items/{item_id}/status` | `components/cocina/ItemRow.vue:24` (with `?token=`) |

Each carries an explicit `# NOTE:` block above its decorator referencing
the consumer file/line and the reason for the exclusion.

`stations.py` follows the same pattern — exclusions to be enumerated when
E2.7 (#191) lands.

## §2. `/invitations/accept` is token-public

`POST /invitations/accept` accepts an invitation by token — the invitee has
no operator session yet. The endpoint is already in the middleware allowlist.
When wiring `EQUIPO` in E2.12 (#196), exclude `/accept` and gate the other
3 endpoints (`send`, `list`, `cancel`) only.

## §3. `payment_methods.py` exports two routers

```python
finanzas_router = APIRouter(prefix="/finanzas/metodos-pago", ...)  # 7 endpoints
pos_router      = APIRouter(prefix="/pos/payment-methods", ...)    # 1 endpoint
```

Both are mounted in `main.py`. Wiring strategy:

- `finanzas_router` → `Depends(require_module(Module.FINANZAS))` → goes in **E2.10** (#198).
- `pos_router` → `Depends(require_module(Module.POS))` → goes in **E2.3** (#188).

Easier than splitting per-endpoint because the file already separates them
into distinct router objects.

## §4. `tenant_config.py` — endpoint-level split required

The 15 endpoints in this router fall into two categories. The wiring PRs
(E2.7 for OPERACIONES, E2.15 for MI_NEGOCIO) will enumerate them, but the
buckets are:

- **OPERACIONES** (toggles operativos):
  - KDS toggles (kds_enabled, expediter_enabled)
  - Comandas toggle (comandas_enabled)
  - Auto-select Genérico toggle, etc.
  - Anything that affects how POS / kitchen / tables operate day-to-day.

- **MI_NEGOCIO** (perfil del negocio):
  - Slug, brand name, descripción
  - Horarios de atención
  - Información fiscal (NIT, regime, etc.)
  - Datos de contacto

Recommendation: do E2.7 and E2.15 in a **single PR** that touches the file
twice but keeps the diff cohesive. Otherwise the file ends up half-gated
between two PRs and reviewers lose context.

## §5. Pattern: delete admin endpoints with no UI consumer instead of gating

When auditing a router for E2.X, if the router exposes admin/maintenance
endpoints that have **zero consumers** across:

- `front_nuxt/` (pages, components, composables, layouts)
- `api-warolabs/` (other routers, services, scripts)
- `postman/` collections
- `tests/`

→ **Delete the router** instead of wiring `require_module()` on it.

Precedents:
- **#185 (E2.14)** — deleted the entire `/admin/billing/*` surface (10 endpoints + 4 Pydantic models + 9 service methods + the matching `useAdminBilling` composable). Added speculatively in #61, never wired into a workflow.
- **#187 (E2.5)** — deleted `admin_orders.py` (1 endpoint, `POST /admin/orders/backfill-order-ingredients`). Added in #105 as a one-shot backfill tool, never exposed in any UI.

Rationale: gating dead code adds wire complexity without security value. If the functionality is needed later, the right move is to rebuild the endpoint with proper gating from day 1, paired with the UI that actually consumes it. The deleted SQL/logic remains in git history (referenced commits in the deletion PR) so it can be reused as a transient psql one-off if ever needed.

How to verify a candidate is dead before deleting:

```bash
# from each repo:
grep -rn '<endpoint-path>' . --include='*.vue' --include='*.ts' --include='*.js' --include='*.py' --include='*.json'
# expect: only the router file itself + main.py mount
```

## §6. `auth.py` and `webhooks.py` — explicit skips

- `auth.py` (`/auth/*` — login, magic link, session, sign-in flow) must run
  BEFORE any session exists. Adding `require_module()` would create a
  chicken-and-egg deadlock. Already in the middleware `public_endpoints`
  allowlist.
- `webhooks.py` (`/api/webhooks/*` — bridge to api-facturacion) is invoked
  by an external service, signature-verified inside the handler. No session
  cookie present. Already in the middleware allowlist.

These two files **never** receive `require_module()`. Document explicitly in
each future PR description that these are intentional skips.

## §7. Ambiguity: `notifications.py` could be POS or OPERACIONES

The router serves `/notifications/*` for the operator UI (SSE stream of
restaurant events: new order, comanda ready, etc.). Today it's classified as
**POS** because the underlying events originate from POS / table / kitchen
flows. Alternatives considered:

- **OPERACIONES** — if the cook / station-only role needs to see them. KITCHEN
  default matrix today only includes DESPACHO, so a kitchen role under enforce
  would be denied access to notifications.
- **mixed** — if notification types should differ per role.

**Decision applied:** classify as POS. Revisit during shadow-mode rollout if
logs show kitchen / supervisor roles being denied for legitimate use.

---

## How to use this doc (for E2.3 → E2.16 implementers)

1. Open this file alongside the issue you're working (e.g. #188 for E2.3 / POS).
2. Find the router rows that match your sub-task's module(s).
3. For each router:
   - Read the file in `app/routers/<file>.py`.
   - Add `from fastapi import Depends` and `from app.core.permissions import Module, require_module` to the imports.
   - Add `dependencies=[Depends(require_module(Module.<X>))]` to each `@router.method(...)` decorator.
   - Skip any endpoint flagged in this doc (KDS-public, /invitations/accept, etc.) with an explicit `# NOTE:` comment above its decorator.
4. Add 2 smoke tests per sub-task (allowed role + denied role under `enforce`),
   following the pattern in `tests/test_billing_permissions.py`.
5. Run the full backend test suite — all 50+ permission tests must still pass.
6. Update the **Status** column of the Coverage Summary in this doc to mark
   the sub-task as done.
7. Open the PR with the body documenting:
   - Which routers were wired.
   - Any endpoint excluded with reason.
   - Smoke test results.
   - Reference back to this doc.

---

## Open questions / follow-ups

- **API key callers (public_api, v1_ordering, INTEGRACIONES):** today their
  pseudo-session built by middleware has `role=None`. After enforce, every
  API-key call would 403. Either (a) plumb a role into `validate_api_key`
  output, or (b) skip these from `require_module` entirely with explicit
  `# NOTE:`. Decide in E2.13 (#197).
- **Reviewer with product context:** the table reflects the auditor's best
  interpretation. A subsequent PR can re-mapping any router based on
  feedback — the doc is versionable.
- **EVENTOS (Module.EVENTOS):** placeholder in the enum, no router today.
  Consider removing the enum entry or keeping it as forward-compat. Decide
  in E2.16 (#200).

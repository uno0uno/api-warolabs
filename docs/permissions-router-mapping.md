# Permissions: Router → Module Mapping

**Status:** Source of truth for Epic 2 (#164) wiring sub-tasks (E2.3 → E2.16).
**Origin:** [#186 audit](https://github.com/uno0uno/api-warolabs/issues/186).
**Last updated:** 2026-05-11 (post #191/#192 OPERACIONES/MI_NEGOCIO + #210 operaciones-context toggles + #212 EVENTOS removed + #193 ANALITICA + #194 FACTURACION + #195 ABASTECIMIENTO + #196 EQUIPO + #197 INTEGRACIONES done; v1_ordering.py count fix 7→17; added §9 self-service exclusions in tenants.py and §10 api-key bypass via require_module early-return).

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

## Authoritative Table — 53 routers

| router_file | mount_prefix | endpoints | module | auth_today | notes |
|---|---|---|---|---|---|
| `accounting.py` | `/accounting` | 13 | **FINANZAS** | session | DONE en #198. Chart of accounts CRUD, balance, P&L |
| `address_profile.py` | `/online/addresses` | 6 | **public** | none | Direcciones de delivery (clientes online) |
| `admin_ingredients.py` | `/admin/ingredients` | 6 | **ABASTECIMIENTO** | session | Catálogo global de ingredientes. DONE en #195. |
| `analytics.py` | `/analytics` | 6 | **ANALITICA** | session | Dashboard analítico, alertas. DONE en #193. |
| `api_tokens.py` | `/api-tokens` | 6 | **INTEGRACIONES** | session | API token CRUD + scopes. DONE en #197. |
| `articles.py` | `/blog` | 3 | **public** | none | Blog público (lista + detalle) |
| `auth.py` | `/auth` | 7 | **skip** | none | Login/sesión — corre SIN sesión |
| `billing.py` | `/billing` | 10 | **MI_PLAN** | mixed | DONE en #185. Webhook + cron skip con `# NOTE:` |
| `cartera.py` | `/cartera` | 4 | **FINANZAS** | session | DONE en #198. Cartera (cuentas por cobrar) |
| `categories.py` | `/menu/categories` | 2 | **MENU** | session | Listado con filtro global+tenant |
| `cierre.py` | `/cierre` | 9 | **FINANZAS** | session | DONE en #198. Cierre X/Z diario |
| `comandas.py` | `/api/comandas` | 11 | **POS** | session | KDS lifecycle. ⚠️ KDS-public paths via `?token=` middleware quedan sin gate (ver §1) |
| `combos.py` | `/menu/combos` | 6 | **MENU** | session | Combo CRUD |
| `credit.py` | `/credit` | 3 | **FINANZAS** | session | DONE en #198. Pagos a crédito |
| `customer_portal.py` | `/customer` | 7 | **public** | none | Portal del cliente final (JWT customer, no sesión de operador) |
| `customers.py` | `/customers` | 6 | **VENTAS** | session | Búsqueda + perfil de clientes (operador) |
| `documents.py` | `/api/documents` | 4 | **FACTURACION** | session | Documentos electrónicos (lista, PDF/XML). DONE en #194. |
| `expenses.py` | `/finance/expenses` | 14 | **FINANZAS** | session | DONE en #198. CRUD de gastos + categorías |
| `facturacion.py` | `/api/acquirer + /api/facturacion + /api/payroll` | 5 | **FACTURACION** | session | 3 sub-routers (acquirer 1ep, catalog 1ep, payroll 3eps) — todos FACTURACION, todos stubs 503 hasta wired api-facturacion (#129). DONE en #194. |
| `financial.py` | (sin prefix) | 3 | **FINANZAS** | session | DONE en #198. TIR, rentabilidad de productos |
| `ingredient_purchase_units.py` | `/suppliers/ingredient-purchase-units` | 6 | **ABASTECIMIENTO** | session | Unidades de compra. DONE en #195. |
| `ingredients.py` | `/suppliers/ingredients` | 10 | **ABASTECIMIENTO** | session | Custom ingredients + catálogo. DONE en #195. |
| `inventory.py` | `/inventory` | 4 | **ABASTECIMIENTO** | session | Stock + ajustes. DONE en #195. |
| `invitations.py` | `/invitations` | 4 | **EQUIPO** | session | ⚠️ `/invitations/accept` es token-público (ver §2). DONE en #196 (3 of 4 gated). |
| `invoices.py` | `/api/invoices` | 4 | **FACTURACION** | session | Notas crédito/débito, RADIAN (stubs 503). DONE en #194. |
| `leads.py` | `/leads` | 2 | **public** | none | Captura de leads (homepage) |
| `menu.py` | `/menu` | 1 | **MENU** | session | Name-check genérico |
| `modifiers.py` | `/menu/modifier-groups` | 6 | **MENU** | session | Grupos de modificadores |
| `notifications.py` | `/notifications` | 4 | **POS** | session | Notificaciones operador (SSE) — ver ambigüedad en §7 |
| `online_cart.py` | `/online/cart` | 10 | **public** | none | Carrito online (anónimo, session_id) |
| `online_orders.py` | `/online/orders` | 4 | **VENTAS** | session | Operador gestiona pedidos online |
| `online_verification.py` | `/online/otp` | 3 | **public** | none | OTP por email del cliente |
| `orders.py` | `/orders` | 18 | **VENTAS** | session | Dashboard de ventas, métricas |
| `payment_methods.py` | `/finanzas/metodos-pago + /pos/payment-methods` | 10 | **mixed** | session | DONE: finanzas_router → FINANZAS en #198 (7 endpoints); pos_router → POS en #188 (1 endpoint). Ver §3 |
| `pos_cart.py` | `/pos/cart` | 11 | **POS** | session | Carrito POS, checkout |
| `products.py` | `/menu/products` | 7 | **MENU** | session | Producto CRUD + receta + imagen |
| `public_api.py` | `/v1` | 25 | **INTEGRACIONES** | api_key | API pública con API key (clientes externos). DONE en #197 (api-key callers bypass via early-return, ver §10). |
| `public_restaurant.py` | `/public/restaurant` | 4 | **public** | none | Lista + detalle por slug |
| `purchases.py` | `/suppliers/purchases` | 26 | **ABASTECIMIENTO** | session | Compras + estados + factura. DONE en #195 (largest router in Epic). |
| `recipe_bases.py` | `/menu/recipe-bases` | 5 | **MENU** | session | Templates de receta |
| `salaries.py` | `/salaries` | 29 | **FINANZAS** | session | DONE en #198 (router más grande del Epic). Nómina + prima + cesantías + PILA |
| `stations.py` | `/api/stations` | 15 | **OPERACIONES** | session | Estaciones de cocina + routing. ⚠️ `GET /{station_id}` excluido (KDS público, ver §1). DONE en #191. |
| `supplier_portal.py` | `/supplier-portal` | 8 | **public** | token | Portal del proveedor (token, no sesión) |
| `suppliers.py` | `/suppliers/providers` | 10 | **ABASTECIMIENTO** | session | Proveedor CRUD. DONE en #195. |
| `support_documents.py` | `/api/support-documents` | 2 | **FACTURACION** | session | DIAN documento soporte (stubs 503). DONE en #194. |
| `tables.py` | `/tables` | 19 | **POS** | session | Mesas + tab + sesión de mesa |
| `tenant_config.py` | `/api/tenant` | 15 | **MI_NEGOCIO** | session | Owner-only. POS consume `/api/pos/restaurant-context` aggregator (ver §4). DONE en #192. |
| `pos_context.py` | `/pos/restaurant-context` | 1 | **POS** | session | BFF-style aggregator de tenant context para POS. Introducido en E2.7/E2.15. |
| `operaciones_context.py` | `/operaciones/restaurant-context + /operaciones/toggles/*` | 6 | **OPERACIONES** | session | Aggregator + 5 PATCH toggle endpoints (kds, comandas, expediter, tables, auto-select-generic). Introducido en #210 (enforce prep). |
| `tenants.py` | `/tenants` | 5 | **EQUIPO** | session | Tenant create + member CRUD. ⚠️ `POST ""` y `GET /user-tenants` excluidos (self-service, ver §9). DONE en #196 (3 of 5 gated). |
| `v1_ordering.py` | `/v1/cart + /v1/addresses + /v1/otp + /v1/customer + /v1/product` | 17 | **INTEGRACIONES** | api_key | V1 ordering API (5 sub-routers: cart 7, address 5, otp 3, customer 1, product 1). DONE en #197 (count fix 7→17; api-key bypass §10). |
| `waros.py` | `/admin/waros` | 8 | **POS** | session | Sistema de loyalty (puntos WaRo) |
| `webhooks.py` | `/api/webhooks` | 1 | **skip** | signature | Bridge a api-facturacion (signature-verified, no sesión) |

---

## Coverage Summary

Total: **53 routers**, **~413 endpoints**, **13 modules** (post #197 corrected v1_ordering.py count 7→17, +10 endpoints; post #194 corrected facturacion.py count 3→5, +2 endpoints; was 14 modules before #212 dropped `EVENTOS`; was 52/395 routers/endpoints after #191/#192 + added `operaciones_context.py` in #210 for OPERACIONES aggregator + toggles).

| Module | Routers | Sub-task | Status |
|---|---|---|---|
| **POS** | comandas, notifications, pos_cart, tables, waros + payment_methods/pos | 5+1 | ✅ E2.3 (#188) — DONE (51 endpoints gated, 3 KDS-direct excluded) |
| **VENTAS** | customers, online_orders, orders | 3 | ✅ E2.4 (#189) — DONE (28 endpoints gated, no exclusions) |
| **DESPACHO** | (no routers — placeholder) | 0 | ✅ E2.5 (#187) — DONE (deleted dead `admin_orders.py`) |
| **MENU** | categories, combos, menu, modifiers, products, recipe_bases | 6 | E2.6 (#190) — pending |
| **OPERACIONES** | stations, operaciones_context | 2 | ✅ E2.7 (#191) + #210 — DONE (14 stations endpoints + 6 operaciones-context endpoints, 1 KDS-public excluded) |
| **ABASTECIMIENTO** | admin_ingredients, ingredient_purchase_units, ingredients, inventory, purchases, suppliers | 6 | ✅ E2.8 (#195) — DONE (62 endpoints gated; largest batch in Epic; no exclusions) |
| **ANALITICA** | analytics | 1 | ✅ E2.9 (#193) — DONE (6 endpoints gated; `articles.py` confirmed public, stays ungated) |
| **FINANZAS** | accounting, cartera, cierre, credit, expenses, financial, salaries + payment_methods/finanzas | 7+1 | ✅ E2.10 (#198) — DONE (82 endpoints gated; largest batch in Epic) |
| **FACTURACION** | documents, facturacion (3 sub-routers), invoices, support_documents | 4 | ✅ E2.11 (#194) — DONE (15 endpoints gated: 4 documents + 5 facturacion + 4 invoices + 2 support_documents; 12 of 15 are stubs awaiting api-facturacion #129) |
| **EQUIPO** | invitations (excl. /accept), tenants (excl. POST "" + /user-tenants) | 2 | ✅ E2.12 (#196) — DONE (6 endpoints gated; 3 self-service / public excluded) |
| **INTEGRACIONES** | api_tokens, public_api, v1_ordering | 3 | ✅ E2.13 (#197) — DONE (48 endpoints gated; api-key callers bypass via require_module early-return on invalid session, ver §10) |
| **MI_PLAN** | billing | 1 | ✅ E2.14 (#185, PR #202) — DONE |
| **MI_NEGOCIO** | tenant_config | 1 | ✅ E2.15 (#192) — DONE (15 endpoints gated, owner-only — ADMIN/SUPERVISOR stripped of MI_NEGOCIO) |
| ~~**EVENTOS**~~ | — | — | ✅ E2.16 (#199 / PR #212) — DONE (`Module.EVENTOS` removed from enum; Eventos lives in warotickets.com, external product) |
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
no operator session yet. The endpoint is already in the middleware allowlist
(`app/main.py:59`). Excluded from the EQUIPO gate in #196 with an explicit
`# NOTE:` block. Frontend consumer: `pages/auth/accept-invitation.vue:174`.

## §3. `payment_methods.py` exports two routers

```python
finanzas_router = APIRouter(prefix="/finanzas/metodos-pago", ...)  # 7 endpoints
pos_router      = APIRouter(prefix="/pos/payment-methods", ...)    # 1 endpoint
```

Both are mounted in `main.py`. Wiring done:

- ✅ `finanzas_router` → `Depends(require_module(Module.FINANZAS))` → **E2.10** (#198), all 7 endpoints.
- ✅ `pos_router` → `Depends(require_module(Module.POS))` → **E2.3** (#188), 1 endpoint.

Easier than splitting per-endpoint because the file already separates them
into distinct router objects. Regression test in `tests/test_finanzas_permissions.py::test_cashier_role_passes_pos_router_under_enforce_regression` guards against accidental rewrites of the POS gate by future FINANZAS bulk-regex passes.

## §4. `tenant_config.py` — endpoint-level split required

**Resolved in #191/#192 — the per-endpoint split is not possible.**

When the audit was first written we expected `tenant_config.py` to have
dedicated endpoints for the operational toggles (KDS, comandas, expediter)
that would naturally bucket under OPERACIONES, separate from the brand /
fiscal / DIAN endpoints under MI_NEGOCIO. Reading the file refuted that:
the operational toggles are **columns** on `tenant_public_profiles`,
written through the same `PUT/PATCH /api/tenant/public-profile` payload as
brand fields. A single request can update `slug` and `kds_enabled` at once
— there is no endpoint to gate separately.

**Decision applied in E2.7 + E2.15 (single PR):**

1. `tenant_config.py` gated **entirely** under `Module.MI_NEGOCIO`.
2. `Module.MI_NEGOCIO` is **owner-only** by business rule — ADMIN and
   SUPERVISOR were stripped of MI_NEGOCIO in `DEFAULT_ROLE_MODULES`.
3. POS used to read 4 `/api/tenant/*` endpoints (`public-profile`,
   `fiscal-data`, `tax-config`, `invoicing-readiness`); enforcing owner-only
   MI_NEGOCIO would have 403ed every cashier. Solution: a new
   **BFF-style scoped endpoint** `GET /api/pos/restaurant-context`
   (`app/routers/pos_context.py`) gated under `Module.POS`. It returns the
   aggregated subset POS needs in a single payload.
4. POS frontend (`pages/pos/index.vue`, `pages/pos/checkout.vue`,
   `composables/useInvoicingReadiness.ts`) cuts over to the new endpoint.

This pattern (audience-scoped aggregator gated under the consumer's module,
private endpoints kept strict) follows Stripe (`/v1/accounts/me` vs full
admin endpoints) and GitHub (`/user` vs `/users/{name}/admin/*`).

If future operational toggles need a dedicated endpoint surface, the right
move is to add them as new, individually-gated endpoints (e.g.
`PATCH /api/tenant/toggles/kds`) rather than re-bucketing existing ones.

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

## §9. `tenants.py` — self-service endpoints excluded from EQUIPO

EQUIPO is owner-only by matrix, but 2 of the 5 endpoints in `tenants.py`
serve cross-role audiences and were excluded from the gate in #196 with
explicit `# NOTE:` blocks:

- **`POST /tenants`** — onboarding / add-new-tenant flow. The caller may
  have no current tenant (role=null) or a non-owner role wanting to start
  their own tenant. The service makes the caller superuser of the newly
  created tenant. Gating under EQUIPO would block legitimate signup.
- **`GET /tenants/user-tenants`** — sidebar tenant switcher. Called by
  every authenticated user from `stores/tenants.ts:59` regardless of role,
  populating `DashboardTenantSelector.vue`. Gating under owner-only EQUIPO
  would break the switcher for cashier / kitchen / admin / supervisor with
  multiple tenant memberships.

The remaining 3 endpoints in `tenants.py` (`/members` GET + DELETE + role
PUT) are correctly gated under EQUIPO since they are admin-only team
management operations consumed by `pages/equipo/miembros.vue`.

A regression test in `tests/test_equipo_permissions.py::test_cashier_passes_user_tenants_exclusion_under_enforce`
guards against accidental future gating of `/user-tenants`.

## §10. API-key callers bypass `require_module` via early-return (#197)

`public_api.py` (25 endpoints) and `v1_ordering.py` (17 endpoints across 5
sub-routers) are authenticated via API keys (`Authorization: Bearer waro_sk_...`
or `X-API-Key` header). The middleware at `app/core/middleware.py:163-194`
validates the key and sets `request.state.tenant_context` — **but never sets
`request.state.session_context`**.

`get_session_context(request)` (middleware.py:528-532) returns an empty
`SessionContext()` for API-key requests, which has `is_valid=False`.
`require_module()` short-circuits on that condition (permissions.py:339-343):

> "Sessions that aren't valid at all return early so `require_valid_session`
> (still called inside handlers) can raise 401 with its own message."

**Effect**: gating `/api-tokens`, `/v1/*` endpoints under INTEGRACIONES is
safe — the gate is a complete no-op for API-key calls, which pass through
to the handler. The handler then runs `validate_api_key_auth(request, scope)`
which enforces scope-based authorization at the token level (`read`, `write`,
`orders:read`, `products:write`, etc.).

**Defense in depth**:
- Outer gate (`require_module`) — gates session-authenticated callers
- Inner check (`validate_api_key_auth`) — enforces scopes on API-key callers
- Both coexist without conflict. The gate is a no-op for API-key flow; the
  scope check is a no-op for session flow.

This pattern is **forward-prep**: if any of these endpoints is ever called
by an operator session (instead of API key), the gate enforces INTEGRACIONES
correctly without needing a second-pass PR.

**Test guarding the early-return**:
\`tests/test_integraciones_permissions.py::test_api_key_request_bypasses_gate_under_enforce\`
asserts that a request with no SessionContext reaches `/v1/cart/batch` under
enforce mode. If anyone later tightens the gate to deny invalid sessions
instead of bypassing them, every API-key caller in production would 403;
this test catches it before merge.

The audit doc's earlier "Open questions" entry asking whether to plumb a role
into API keys or skip those routers is **resolved by this finding**: neither
is necessary.

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

## §8. FINANZAS caveats from #198

Two decisions intentionally NOT made in E2.10 (#198):

1. **SUPERVISOR does not have FINANZAS** in the matrix (`app/core/permissions.py:99-108`).
   The issue body of #198 says "FINANZAS defaults to admin/supervisor", but the matrix
   on `main` is the source of truth: only OWNER and ADMIN hold FINANZAS today. After
   #198 merge, a SUPERVISOR session hitting any FINANZAS endpoint → 403. If product
   later decides supervisors need read access (e.g. cierre dashboard), open a separate
   issue and add `Module.FINANZAS` to `Role.SUPERVISOR` defaults — single-line matrix
   change, no router edits required.

2. **Salaries owner-only is a follow-up**, not part of #198. The issue body suggests
   that creating / modifying salary configuration (`POST /salaries/employees/{id}/config`,
   `POST /salaries/payments`, etc.) should be owner-only — not admin. That requires
   per-endpoint `require_role(Role.OWNER)` style restrictions inside the salaries router,
   which is a different mechanism than module gating. Track separately after shadow logs
   surface any admin/supervisor activity on salary endpoints.

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
- **EVENTOS:** resolved in #199 — Eventos lives in a separate product
  (warotickets.com), not in this codebase. `Module.EVENTOS` was removed
  from the enum and from `Role.ADMIN`'s default set. The only Eventos
  surface in WARO Colombia is a sidebar `<a>` to `https://warotickets.com/gestion/eventos`,
  conditioned to owner role. Three dead frontend components
  (`EventForm.vue`, `EventWizard.vue`, `EventWizardComplete.vue`,
  ~2060 lines combined) were deleted at the same time — they POSTed to a
  non-existent `/api/events` endpoint and had zero consumers across pages,
  layouts, or app.vue.

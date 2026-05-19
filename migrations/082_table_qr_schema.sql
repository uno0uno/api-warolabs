-- 082_table_qr_schema.sql
-- Issue api-warolabs#265 — Table QR ordering DB foundation (epic warocol.com#710)
--
-- ADD-only: feature flag, per-table QR token/toggle, product visibility flag,
-- and pending customer request queue. Token generation and API land in #266+.
-- Safe to re-run: ADD COLUMN IF NOT EXISTS, CREATE TABLE IF NOT EXISTS.

-- ─────────────────────────────────────────────
-- BLOCK A: Feature flag on tenant_public_profiles
-- ─────────────────────────────────────────────

ALTER TABLE tenant_public_profiles
    ADD COLUMN IF NOT EXISTS table_qr_module_enabled BOOLEAN NOT NULL DEFAULT false;

COMMENT ON COLUMN tenant_public_profiles.table_qr_module_enabled IS
    'Table QR ordering module (warocol.com#710). When true, tenant can enable '
    'per-table static QR links for diner self-order with staff confirmation. '
    'Default false preserves current behaviour.';

-- ─────────────────────────────────────────────
-- BLOCK B: Per-table public token + enable toggle
-- ─────────────────────────────────────────────

ALTER TABLE tables
    ADD COLUMN IF NOT EXISTS qr_public_token VARCHAR(64),
    ADD COLUMN IF NOT EXISTS qr_enabled BOOLEAN NOT NULL DEFAULT false;

COMMENT ON COLUMN tables.qr_public_token IS
    'Opaque public token for Table QR URL (api-warolabs#265). Generated via '
    'secrets.token_urlsafe(32) in #266 — not derivable from table_id. NULL '
    'until first enable/create. Globally unique when set.';

COMMENT ON COLUMN tables.qr_enabled IS
    'Per-table QR toggle (api-warolabs#265). When false, public resolve '
    'rejects access even if token is set. Default false.';

CREATE UNIQUE INDEX IF NOT EXISTS idx_tables_qr_public_token
    ON tables (qr_public_token)
    WHERE (qr_public_token IS NOT NULL);

-- ─────────────────────────────────────────────
-- BLOCK C: Product visibility for public QR menu
-- ─────────────────────────────────────────────

ALTER TABLE product
    ADD COLUMN IF NOT EXISTS is_available_table_qr BOOLEAN NOT NULL DEFAULT false;

COMMENT ON COLUMN product.is_available_table_qr IS
    'Whether product appears on the Table QR public menu (warocol.com#710). '
    'Independent of is_available_online (domicilios). Default false.';

CREATE INDEX IF NOT EXISTS idx_product_available_table_qr
    ON product (tenant_id, is_available_table_qr)
    WHERE (is_available_table_qr = true);

-- ─────────────────────────────────────────────
-- BLOCK D: Pending customer requests (staff confirms in Despacho)
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS table_qr_requests (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    table_id            UUID NOT NULL REFERENCES tables(id) ON DELETE CASCADE,
    status              VARCHAR(20) NOT NULL DEFAULT 'pending',
    items               JSONB NOT NULL,
    payment_method      VARCHAR(50),
    payment_method_id   UUID REFERENCES payment_methods(id) ON DELETE SET NULL,
    customer_notes      TEXT,
    session_id          UUID REFERENCES table_sessions(id) ON DELETE SET NULL,
    accepted_by         UUID REFERENCES tenant_members(id) ON DELETE SET NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    accepted_at         TIMESTAMPTZ,
    rejected_at         TIMESTAMPTZ,
    CONSTRAINT chk_table_qr_request_status
        CHECK (status IN ('pending', 'accepted', 'rejected'))
);

CREATE INDEX IF NOT EXISTS idx_tqr_tenant_status
    ON table_qr_requests (tenant_id, status);

CREATE INDEX IF NOT EXISTS idx_tqr_table_pending
    ON table_qr_requests (table_id, status)
    WHERE (status = 'pending');

COMMENT ON TABLE table_qr_requests IS
    'Customer Table QR orders awaiting staff confirmation (warocol.com#710). '
    'Submit (#267) creates pending rows; accept (#268) opens session, merges '
    'to POS tab, and fires comandas. Multiple pending rows per table allowed.';

COMMENT ON COLUMN table_qr_requests.items IS
    'Line items snapshot at submit: product_id, quantity, unit_price, '
    'modifiers, notes — aligned with POS TabItem (app/routers/tables.py).';

COMMENT ON COLUMN table_qr_requests.payment_method_id IS
    'Customer payment intent FK. #267 must validate tenant_id match before insert.';

COMMENT ON COLUMN table_qr_requests.session_id IS
    'Set on accept (#268) when table session is opened or reused.';

COMMENT ON COLUMN table_qr_requests.accepted_by IS
    'tenant_members.id of staff who accepted the request (#268).';

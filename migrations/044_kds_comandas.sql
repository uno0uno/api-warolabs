-- Migration 044: KDS (Kitchen Display System) — base data model
-- Creates kitchen_stations, tenant_category_stations, comandas, comanda_items tables.
-- Adds fulfillment_status to order_items, station routing to product,
-- and comandas_enabled/kds_enabled feature flags to tenant_public_profiles.
-- Safe: ADD/CREATE only — no DROP, no ALTER on existing columns.
-- Issue: https://github.com/uno0uno/warocol.com/issues/410

-- ─────────────────────────────────────────────
-- BLOCK A: New tables
-- ─────────────────────────────────────────────

-- Preparation points per tenant (Cocina, Barra, Panadería, etc.)
CREATE TABLE IF NOT EXISTS kitchen_stations (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name                  VARCHAR(100) NOT NULL,
    kitchen_name          VARCHAR(50),                         -- short name shown on KDS screen (e.g. "COC", "BAR")
    color                 VARCHAR(7) NOT NULL DEFAULT '#6B7280', -- hex color for UI identification
    is_active             BOOLEAN NOT NULL DEFAULT true,
    display_order         INTEGER NOT NULL DEFAULT 0,
    alert_threshold_1_min INTEGER NOT NULL DEFAULT 8,          -- yellow alert after N minutes
    alert_threshold_2_min INTEGER NOT NULL DEFAULT 15,         -- red alert after N minutes
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Per-tenant category → station mapping.
-- NOTE: categories table has NO tenant_id (it is global/shared).
-- A direct FK on categories would create cross-tenant coupling.
-- This junction table lets each tenant map their categories independently.
CREATE TABLE IF NOT EXISTS tenant_category_stations (
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    category_id UUID NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    station_id  UUID NOT NULL REFERENCES kitchen_stations(id) ON DELETE CASCADE,
    PRIMARY KEY (tenant_id, category_id)
);

-- One comanda per order × station per fire event.
-- Multiple comandas can exist for the same order (one per fire round × station).
CREATE TABLE IF NOT EXISTS comandas (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          UUID NOT NULL REFERENCES tenants(id),
    order_id           UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    station_id         UUID NOT NULL REFERENCES kitchen_stations(id),
    comanda_number     INTEGER NOT NULL,                      -- sequential per day per station (e.g. 42 → displayed as "COC-042")
    status             VARCHAR(20) NOT NULL DEFAULT 'pending',
    source_type        VARCHAR(20) NOT NULL,                  -- table | pos | delivery | pickup
    table_display_name VARCHAR(100),                          -- denormalized display: "Mesa 5", "Domicilio #142", "Mostrador"
    notes              TEXT,
    fired_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    ready_at           TIMESTAMPTZ,
    delivered_at       TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_comanda_status CHECK (status IN ('pending', 'preparing', 'ready', 'delivered')),
    CONSTRAINT chk_comanda_source CHECK (source_type IN ('table', 'pos', 'delivery', 'pickup'))
);

-- Line items inside each comanda — snapshot at fire time.
-- Captures kitchen_name, quantity, modifiers independently from order_items.
CREATE TABLE IF NOT EXISTS comanda_items (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    comanda_id         UUID NOT NULL REFERENCES comandas(id) ON DELETE CASCADE,
    order_item_id      UUID NOT NULL REFERENCES order_items(id),
    kitchen_name       VARCHAR(255) NOT NULL,                 -- snapshot: product.kitchen_name ?? product.name
    quantity           NUMERIC(10,4) NOT NULL,
    notes              TEXT,                                  -- snapshot from pos_cart_items.notes at fire time
    modifiers_snapshot JSONB,                                 -- [{"name": "Sin cebolla", "price": 0}, ...]
    status             VARCHAR(20) NOT NULL DEFAULT 'pending',
    ready_at           TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_comanda_item_status CHECK (status IN ('pending', 'ready'))
);

-- ─────────────────────────────────────────────
-- BLOCK B: ALTER existing tables (additive only)
-- ─────────────────────────────────────────────

-- order_items: fulfillment tracking (industry-standard: NEW → HOLD → SENT → READY)
-- Default 'new' backfills all 5,238 existing rows safely (constant default, no table rewrite).
ALTER TABLE order_items ADD COLUMN IF NOT EXISTS fulfillment_status VARCHAR(10) NOT NULL DEFAULT 'new';
ALTER TABLE order_items ADD COLUMN IF NOT EXISTS sent_at            TIMESTAMPTZ;
ALTER TABLE order_items ADD COLUMN IF NOT EXISTS ready_at           TIMESTAMPTZ;

-- product: station override (tier-2 routing) + KDS display name
-- NULL station_id → inherit from tenant_category_stations; still NULL → no comanda generated.
ALTER TABLE product ADD COLUMN IF NOT EXISTS station_id   UUID REFERENCES kitchen_stations(id) ON DELETE SET NULL;
ALTER TABLE product ADD COLUMN IF NOT EXISTS kitchen_name VARCHAR(100);

-- tenant_public_profiles: feature flags (same pattern as existing tables_enabled)
ALTER TABLE tenant_public_profiles ADD COLUMN IF NOT EXISTS comandas_enabled BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE tenant_public_profiles ADD COLUMN IF NOT EXISTS kds_enabled      BOOLEAN NOT NULL DEFAULT false;

-- ─────────────────────────────────────────────
-- BLOCK C: CHECK constraint on fulfillment_status
-- ─────────────────────────────────────────────

-- Add as NOT VALID first (skips full table scan on existing rows), then validate.
-- Safe for production: existing rows all have value 'new' which satisfies the constraint.
-- DO block guards against re-running (IF NOT EXISTS not supported for CHECK in PG < 15).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_order_item_fulfillment'
          AND conrelid = 'order_items'::regclass
    ) THEN
        ALTER TABLE order_items
            ADD CONSTRAINT chk_order_item_fulfillment
            CHECK (fulfillment_status IN ('new', 'hold', 'sent', 'ready')) NOT VALID;
    END IF;
END
$$;

ALTER TABLE order_items VALIDATE CONSTRAINT chk_order_item_fulfillment;

-- ─────────────────────────────────────────────
-- BLOCK D: Indexes
-- ─────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_kitchen_stations_tenant
    ON kitchen_stations(tenant_id);

CREATE INDEX IF NOT EXISTS idx_kitchen_stations_active
    ON kitchen_stations(tenant_id, is_active)
    WHERE is_active = true;

CREATE INDEX IF NOT EXISTS idx_tenant_cat_stations_category
    ON tenant_category_stations(category_id);

CREATE INDEX IF NOT EXISTS idx_comandas_order
    ON comandas(order_id);

-- Primary query path: active comandas for a station (KDS screen)
CREATE INDEX IF NOT EXISTS idx_comandas_station_active
    ON comandas(station_id, status)
    WHERE status IN ('pending', 'preparing', 'ready');

-- Monitor query path: all active comandas for a tenant today
CREATE INDEX IF NOT EXISTS idx_comandas_tenant_date
    ON comandas(tenant_id, fired_at);

CREATE INDEX IF NOT EXISTS idx_comanda_items_comanda
    ON comanda_items(comanda_id);

-- fire_comandas() engine: fast lookup of unfired items
CREATE INDEX IF NOT EXISTS idx_order_items_fulfillment_new
    ON order_items(fulfillment_status)
    WHERE fulfillment_status = 'new';

CREATE INDEX IF NOT EXISTS idx_product_station
    ON product(station_id)
    WHERE station_id IS NOT NULL;

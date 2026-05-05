-- Migration 063: add delivery fields to orders for POS-originated delivery orders
-- Safe: ADD only, no DROP, no destructive ALTER (CLAUDE.md rule)
--
-- Context: pos_cart_service writes to orders with pos_cart_id IS NOT NULL,
-- online_cart_service writes with online_cart_id IS NOT NULL. Storefront
-- delivery info lives on online_carts (delivery_address_id, scheduled_time,
-- delivery_instructions). POS today has no delivery concept; this migration
-- adds the same metadata directly on orders so the POS path can persist it
-- without an intermediate cart that survives checkout.
--
-- scheduled_time is NOT added here — it already exists on orders (used by
-- online_cart_service since the storefront launch).
--
-- shipping_address_id and billing_address_id pre-existing on orders are
-- vestigial (0/13090 rows used, 0 code references, FK to empty addresses
-- table). Left untouched per the additive-only rule. A separate tech-debt
-- issue can clean them up later.

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS delivery_address_id UUID REFERENCES addresses_profile(id);

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS delivery_instructions TEXT;

CREATE INDEX IF NOT EXISTS idx_orders_delivery_address_id
    ON orders(delivery_address_id) WHERE delivery_address_id IS NOT NULL;

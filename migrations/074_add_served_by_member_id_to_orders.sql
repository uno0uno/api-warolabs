-- 074_add_served_by_member_id_to_orders.sql
-- Issue warocol.com#575 — per-order waiter attribution.
--
-- Adds the third (and final) level of the waiter attribution resolver
-- for the WARO waiter family:
--   1. order.served_by_member_id          ← this migration (#575)
--   2. table_session.attended_by_member_id (#574, migration 073)
--   3. table.assigned_member_id            (#573, migration 071)
--
-- Used primarily by bar + counter modes where session-level override
-- doesn't fit naturally:
--   - Bar: session is perpetual, but each order has its own server.
--   - Counter: no session at all, server is per-sale.
--
-- Mesa orders inherit from session/table via the resolver — this
-- column stays NULL unless explicitly overridden.
--
-- ON DELETE SET NULL preserves the order's history if the member
-- is later removed.

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS served_by_member_id UUID NULL
    REFERENCES tenant_members(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_orders_served_by_member
    ON orders(served_by_member_id)
    WHERE served_by_member_id IS NOT NULL;

COMMENT ON COLUMN orders.served_by_member_id IS
    'Per-order waiter attribution (warocol.com#575). Used by bar/counter '
    'modes where mesa-level (#573) and session-level (#574) override do '
    'not fit. Resolver: order > session > table > NULL.';

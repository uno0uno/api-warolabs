-- Migration 049: Add 'cancelled' to comanda_items status + cancelled_at timestamp
-- The existing CHECK from migration 044 (KDS epic) only covers 'pending' and 'ready'.
-- 'cancelled' is needed when a fired order_item is removed from the POS tab.
-- Also adds cancelled_at to mirror the ready_at pattern.
-- Safe: DROP CONSTRAINT + ADD CONSTRAINT + ADD COLUMN only — no data loss.
-- Zero-data-impact: all existing rows have valid status within the new wider set.
-- Issue: https://github.com/uno0uno/warocol.com/issues/431

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_comanda_item_status'
          AND conrelid = 'comanda_items'::regclass
    ) THEN
        ALTER TABLE comanda_items DROP CONSTRAINT chk_comanda_item_status;
    END IF;
END
$$;

ALTER TABLE comanda_items
    ADD CONSTRAINT chk_comanda_item_status
    CHECK (status IN ('pending', 'ready', 'cancelled'));

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'comanda_items' AND column_name = 'cancelled_at'
    ) THEN
        ALTER TABLE comanda_items ADD COLUMN cancelled_at TIMESTAMPTZ;
    END IF;
END
$$;

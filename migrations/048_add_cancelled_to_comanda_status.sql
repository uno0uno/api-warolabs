-- Migration 048: Add 'cancelled' to chk_comanda_status CHECK constraint
-- The existing constraint from 044 covers 4 states: pending, preparing, ready, delivered.
-- 'cancelled' is used by update_comanda_status() (pending → cancelled, preparing → cancelled)
-- but was absent from the DB constraint, causing a constraint violation if ever written.
-- Safe: DROP CONSTRAINT + ADD CONSTRAINT only — no column drops, no data loss.
-- Zero-data-impact: all existing rows have valid values within the new wider set.
-- Issue: https://github.com/uno0uno/warocol.com/issues/416

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_comanda_status'
          AND conrelid = 'comandas'::regclass
    ) THEN
        ALTER TABLE comandas DROP CONSTRAINT chk_comanda_status;
    END IF;
END
$$;

ALTER TABLE comandas
    ADD CONSTRAINT chk_comanda_status
    CHECK (status IN ('pending', 'preparing', 'ready', 'delivered', 'cancelled'));

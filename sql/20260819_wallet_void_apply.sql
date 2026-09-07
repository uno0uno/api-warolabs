-- warocol.com#2360 — restore wallet apply on cancelled / voided sales.
-- Additive CHECK replacement: keep existing types, add void_apply.

ALTER TABLE customer_wallet_movements
    DROP CONSTRAINT IF EXISTS chk_customer_wallet_movement_type;

ALTER TABLE customer_wallet_movements
    ADD CONSTRAINT chk_customer_wallet_movement_type CHECK (
        movement_type IN ('receive', 'apply', 'refund', 'adjust', 'void_apply')
    );

COMMENT ON COLUMN customer_wallet_movements.movement_type IS
    'receive=staff recharge; apply=checkout debit; refund=staff payout; adjust=manual correction; void_apply=restore apply on void/cancel.';

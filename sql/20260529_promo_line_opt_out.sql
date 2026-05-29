-- warocol.com#1003 — per-line promotion opt-out at checkout

ALTER TABLE tenant_public_profiles
    ADD COLUMN IF NOT EXISTS allow_promo_line_opt_out boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN tenant_public_profiles.allow_promo_line_opt_out IS
    'When true, POS checkout shows a per-line toggle to exclude automatic promotions (warocol.com#1003).';

ALTER TABLE pos_cart_items
    ADD COLUMN IF NOT EXISTS promo_opt_out boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN pos_cart_items.promo_opt_out IS
    'Cashier opted this cart line out of automatic promotions at checkout.';

ALTER TABLE order_items
    ADD COLUMN IF NOT EXISTS promo_opt_out boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN order_items.promo_opt_out IS
    'Cashier opted this mesa/tab line out of automatic promotions at checkout.';

-- 084_waro_redemption.sql
-- Issue api-warolabs#370 — WaRos hybrid redemption (B1/B2/B3) at checkout
-- Epic warocol.com#1061 batch 2

-- ─────────────────────────────────────────────
-- gamification_config — redemption + earn flags
-- ─────────────────────────────────────────────

ALTER TABLE gamification_config
    ADD COLUMN IF NOT EXISTS redemption_enabled boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS waros_per_1000_cop integer NOT NULL DEFAULT 100,
    ADD COLUMN IF NOT EXISTS max_redeem_percent_per_order numeric(5,2) NOT NULL DEFAULT 50.00,
    ADD COLUMN IF NOT EXISTS min_waros_to_redeem integer NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS earn_on_wallet_payment boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS earn_base_excludes_waro_redemption boolean NOT NULL DEFAULT true;

COMMENT ON COLUMN gamification_config.redemption_enabled IS
    'When true, customers may redeem WaRos at checkout (api#370).';
COMMENT ON COLUMN gamification_config.waros_per_1000_cop IS
    'WaRos required to redeem 1000 COP (B1 points→COP rate).';
COMMENT ON COLUMN gamification_config.max_redeem_percent_per_order IS
    'Max B1 COP discount as percent of base_canje after manual + B2 fixed off.';
COMMENT ON COLUMN gamification_config.min_waros_to_redeem IS
    'Minimum WaRos for B1 redemption on a single order.';
COMMENT ON COLUMN gamification_config.earn_on_wallet_payment IS
    'When false, wallet-paid portion excluded from earn base (api#370).';
COMMENT ON COLUMN gamification_config.earn_base_excludes_waro_redemption IS
    'When true, waro_redeemed_amount_cop added back to earn eligible subtotal.';

-- ─────────────────────────────────────────────
-- waro_rewards — B2 catalog
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS waro_rewards (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name                VARCHAR(120) NOT NULL,
    reward_type         VARCHAR(20) NOT NULL,
    waros_cost          integer NOT NULL,
    fixed_cop_off       numeric(12,2),
    product_id          UUID REFERENCES product(id) ON DELETE SET NULL,
    is_active           boolean NOT NULL DEFAULT true,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_waro_rewards_type CHECK (
        reward_type IN ('fixed_cop_off', 'free_product')
    ),
    CONSTRAINT chk_waro_rewards_cost_positive CHECK (waros_cost > 0),
    CONSTRAINT chk_waro_rewards_fixed_cop CHECK (
        (reward_type = 'fixed_cop_off' AND fixed_cop_off IS NOT NULL AND fixed_cop_off > 0)
        OR (reward_type = 'free_product' AND product_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_waro_rewards_tenant_active
    ON waro_rewards (tenant_id, is_active);

COMMENT ON TABLE waro_rewards IS
    'WaRos catalog rewards redeemable at checkout (api#370 B2).';

-- ─────────────────────────────────────────────
-- orders — redemption summary columns
-- ─────────────────────────────────────────────

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS waros_redeemed integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS waro_redeemed_amount_cop numeric(12,2) NOT NULL DEFAULT 0;

COMMENT ON COLUMN orders.waros_redeemed IS
    'Total WaRos spent on this order (B1 + B2).';
COMMENT ON COLUMN orders.waro_redeemed_amount_cop IS
    'Total COP discount from WaRo redemption (B1 COP + B2 fixed off).';

-- ─────────────────────────────────────────────
-- order_waro_redemptions — detail rows
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS order_waro_redemptions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id            UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    redemption_type     VARCHAR(20) NOT NULL,
    waros_spent         integer NOT NULL,
    cop_discount        numeric(12,2) NOT NULL DEFAULT 0,
    waro_reward_id      UUID REFERENCES waro_rewards(id) ON DELETE SET NULL,
    order_item_id       UUID REFERENCES order_items(id) ON DELETE SET NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_order_waro_redemptions_type CHECK (
        redemption_type IN ('points_cop', 'reward_fixed_cop', 'reward_free_product')
    ),
    CONSTRAINT chk_order_waro_redemptions_waros CHECK (waros_spent >= 0)
);

CREATE INDEX IF NOT EXISTS idx_order_waro_redemptions_order
    ON order_waro_redemptions (order_id);

-- ─────────────────────────────────────────────
-- order_items — B2 free product source tag
-- ─────────────────────────────────────────────

ALTER TABLE order_items
    ADD COLUMN IF NOT EXISTS line_source varchar(30);

COMMENT ON COLUMN order_items.line_source IS
    'Origin tag for special lines (e.g. waro_reward for B2 free product).';

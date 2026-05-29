-- warocol.com#1011 — tenant promo conflict strategy + type-block defaults

ALTER TABLE tenant_public_profiles
    ADD COLUMN IF NOT EXISTS promo_conflict_strategy text NOT NULL DEFAULT 'priority';

ALTER TABLE tenant_public_profiles
    ADD COLUMN IF NOT EXISTS promo_type_block_map jsonb NOT NULL
    DEFAULT '{"bogo": ["percent_off", "fixed_off"]}'::jsonb;

COMMENT ON COLUMN tenant_public_profiles.promo_conflict_strategy IS
    'Checkout promo conflict winner strategy; priority = highest priority wins (warocol.com#1011).';

COMMENT ON COLUMN tenant_public_profiles.promo_type_block_map IS
    'Map of winning promo_type -> blocked promo_types on the same checkout line (warocol.com#1011).';

ALTER TABLE tenant_public_profiles
    DROP CONSTRAINT IF EXISTS tenant_public_profiles_promo_conflict_strategy_check;

ALTER TABLE tenant_public_profiles
    ADD CONSTRAINT tenant_public_profiles_promo_conflict_strategy_check
    CHECK (promo_conflict_strategy IN ('priority'));

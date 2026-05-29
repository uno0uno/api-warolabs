-- Tenant-scoped POS promotions (warocol.com#980) — greenfield; do NOT reuse legacy promotions.
CREATE TABLE IF NOT EXISTS tenant_promotions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name VARCHAR(120) NOT NULL,
    promo_type TEXT NOT NULL,
    value_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    scope_type TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    is_active BOOLEAN NOT NULL DEFAULT true,
    stackable BOOLEAN NOT NULL DEFAULT false,
    starts_at TIMESTAMPTZ,
    ends_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT tenant_promotions_promo_type_check
        CHECK (promo_type = ANY (ARRAY['percent_off'::text, 'fixed_off'::text, 'bogo'::text])),
    CONSTRAINT tenant_promotions_scope_type_check
        CHECK (scope_type = ANY (ARRAY['all_products'::text, 'categories'::text, 'products'::text])),
    CONSTRAINT tenant_promotions_name_tenant_unique UNIQUE (tenant_id, name)
);

COMMENT ON TABLE tenant_promotions IS
    'Tenant POS promotion rules. v1 conflict: higher priority wins; non-stackable (stackable=false default).';

CREATE INDEX IF NOT EXISTS idx_tenant_promotions_tenant_active
    ON tenant_promotions (tenant_id, is_active, priority DESC);

CREATE TABLE IF NOT EXISTS tenant_promotion_schedules (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    promotion_id UUID NOT NULL REFERENCES tenant_promotions(id) ON DELETE CASCADE,
    days_of_week SMALLINT NOT NULL,
    start_time TIME NOT NULL,
    end_time TIME NOT NULL,
    crosses_midnight BOOLEAN NOT NULL DEFAULT false,
    sort_order SMALLINT NOT NULL DEFAULT 0,
    CONSTRAINT tenant_promotion_schedules_days_check
        CHECK (days_of_week >= 1 AND days_of_week <= 127),
    CONSTRAINT tenant_promotion_schedules_window_check
        CHECK (crosses_midnight OR end_time > start_time)
);

CREATE INDEX IF NOT EXISTS idx_tenant_promotion_schedules_promotion
    ON tenant_promotion_schedules (promotion_id, sort_order);

CREATE TABLE IF NOT EXISTS tenant_promotion_scope_categories (
    promotion_id UUID NOT NULL REFERENCES tenant_promotions(id) ON DELETE CASCADE,
    category_id UUID NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    PRIMARY KEY (promotion_id, category_id)
);

CREATE TABLE IF NOT EXISTS tenant_promotion_scope_products (
    promotion_id UUID NOT NULL REFERENCES tenant_promotions(id) ON DELETE CASCADE,
    product_id UUID NOT NULL REFERENCES product(id) ON DELETE CASCADE,
    PRIMARY KEY (promotion_id, product_id)
);

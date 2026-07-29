-- warocol.com#1883 — menu category → tax line map + exempt set + product override (ADD only).
ALTER TABLE tenant_tax_config
    ADD COLUMN IF NOT EXISTS menu_category_line_map jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE tenant_tax_config
    ADD COLUMN IF NOT EXISTS exempt_menu_category_ids uuid[] NOT NULL DEFAULT '{}'::uuid[];

ALTER TABLE product
    ADD COLUMN IF NOT EXISTS tax_resolution text NOT NULL DEFAULT 'inherit';

ALTER TABLE product
    ADD COLUMN IF NOT EXISTS tax_line_key text NULL;

-- Soft check: inherit | exempt | line (enforced in API).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'product_tax_resolution_check'
    ) THEN
        ALTER TABLE product
            ADD CONSTRAINT product_tax_resolution_check
            CHECK (tax_resolution IN ('inherit', 'exempt', 'line'));
    END IF;
END $$;

-- Issue warocol.com#1845: profile-driven tax_lines + category map.
-- ADD-only: nullable JSONB; CO INC/IVA/liquor columns remain source of truth
-- until packs write explicit lines (wave-1 / commercial).

ALTER TABLE tenant_tax_config
    ADD COLUMN IF NOT EXISTS tax_lines jsonb;

ALTER TABLE tenant_tax_config
    ADD COLUMN IF NOT EXISTS category_map jsonb;

COMMENT ON COLUMN tenant_tax_config.tax_lines IS
    'Optional hospitality tax_lines[]: {key, label, rate, included_in_price, gl_role, ...}. '
    'When null, engine adapts INC/IVA/liquor columns.';

COMMENT ON COLUMN tenant_tax_config.category_map IS
    'Optional product tax_category → tax line key (standard/liquor/exempt). '
    'When null with tax_lines, engine defaults standard+liquor→first line, exempt→null.';

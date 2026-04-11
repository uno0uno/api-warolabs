-- Migration 010: Widen base_quantity from NUMERIC(10,4) to NUMERIC(16,4)
-- NUMERIC(10,4) overflows at 1,000,000 — converting kg→g on large quantities triggers this.
-- NUMERIC(16,4) supports up to 999,999,999,999.9999 (sufficient for any realistic quantity).

ALTER TABLE base_recipe_templates
    ALTER COLUMN base_quantity TYPE NUMERIC(16, 4);

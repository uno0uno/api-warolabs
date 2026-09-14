-- warocol.com#2702 — add rendimiento (yield) to recipe bases for gr/ml portion UX
-- Non-destructive, nullable for backward compat (legacy recipes keep NULL -> legacy x receta)

ALTER TABLE product_base_types
    ADD COLUMN IF NOT EXISTS rendimiento_total NUMERIC(10,2),
    ADD COLUMN IF NOT EXISTS unidad_rendimiento VARCHAR(10) DEFAULT 'ml' CHECK (unidad_rendimiento IN ('ml','gr','und'));

ALTER TABLE base_recipe_templates
    ADD COLUMN IF NOT EXISTS rendimiento_total NUMERIC(10,2),
    ADD COLUMN IF NOT EXISTS unidad_rendimiento VARCHAR(10) DEFAULT 'ml' CHECK (unidad_rendimiento IN ('ml','gr','und'));

-- warocol.com#1668: managed, tenant-aware warehouse ingredient categories.
CREATE EXTENSION IF NOT EXISTS unaccent;

CREATE OR REPLACE FUNCTION normalize_warehouse_category_name(value TEXT)
RETURNS TEXT
LANGUAGE SQL
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT LOWER(
        REGEXP_REPLACE(
            BTRIM(public.unaccent(COALESCE(value, ''))),
            '\s+',
            ' ',
            'g'
        )
    )
$$;

CREATE TABLE IF NOT EXISTS warehouse_categories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    normalized_name VARCHAR(255) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT warehouse_categories_name_nonempty
        CHECK (normalized_name <> '')
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_warehouse_categories_global_name
    ON warehouse_categories (normalized_name)
    WHERE tenant_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_warehouse_categories_tenant_name
    ON warehouse_categories (tenant_id, normalized_name)
    WHERE tenant_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_warehouse_categories_tenant_active
    ON warehouse_categories (tenant_id, is_active, normalized_name);

ALTER TABLE ingredients
    ADD COLUMN IF NOT EXISTS warehouse_category_id UUID;

ALTER TABLE ingredients
    DROP CONSTRAINT IF EXISTS ingredients_warehouse_category_id_fkey;
ALTER TABLE ingredients
    ADD CONSTRAINT ingredients_warehouse_category_id_fkey
    FOREIGN KEY (warehouse_category_id)
    REFERENCES warehouse_categories(id)
    ON DELETE RESTRICT;

CREATE INDEX IF NOT EXISTS idx_ingredients_warehouse_category_id
    ON ingredients (warehouse_category_id);

WITH grouped AS (
    SELECT
        normalize_warehouse_category_name(category) AS normalized_name,
        REGEXP_REPLACE(BTRIM(category), '\s+', ' ', 'g') AS display_name,
        COUNT(*) AS usage_count
    FROM ingredients
    WHERE tenant_id IS NULL
      AND NULLIF(BTRIM(category), '') IS NOT NULL
    GROUP BY 1, 2
),
ranked AS (
    SELECT *,
           ROW_NUMBER() OVER (
               PARTITION BY normalized_name
               ORDER BY usage_count DESC, LOWER(display_name), display_name
           ) AS rank
    FROM grouped
)
INSERT INTO warehouse_categories (tenant_id, name, normalized_name)
SELECT NULL, display_name, normalized_name
FROM ranked
WHERE rank = 1
ON CONFLICT (normalized_name) WHERE tenant_id IS NULL DO NOTHING;

WITH grouped AS (
    SELECT
        tenant_id,
        normalize_warehouse_category_name(category) AS normalized_name,
        REGEXP_REPLACE(BTRIM(category), '\s+', ' ', 'g') AS display_name,
        COUNT(*) AS usage_count
    FROM ingredients
    WHERE tenant_id IS NOT NULL
      AND NULLIF(BTRIM(category), '') IS NOT NULL
    GROUP BY 1, 2, 3
),
ranked AS (
    SELECT *,
           ROW_NUMBER() OVER (
               PARTITION BY tenant_id, normalized_name
               ORDER BY usage_count DESC, LOWER(display_name), display_name
           ) AS rank
    FROM grouped
)
INSERT INTO warehouse_categories (tenant_id, name, normalized_name)
SELECT source.tenant_id, source.display_name, source.normalized_name
FROM ranked source
WHERE source.rank = 1
  AND NOT EXISTS (
      SELECT 1
      FROM warehouse_categories global_category
      WHERE global_category.tenant_id IS NULL
        AND global_category.normalized_name = source.normalized_name
  )
ON CONFLICT (tenant_id, normalized_name)
    WHERE tenant_id IS NOT NULL
    DO NOTHING;

UPDATE ingredients ingredient
SET warehouse_category_id = (
    SELECT category.id
    FROM warehouse_categories category
    WHERE category.normalized_name =
              normalize_warehouse_category_name(ingredient.category)
      AND (
          category.tenant_id IS NULL
          OR category.tenant_id = ingredient.tenant_id
      )
    ORDER BY category.tenant_id NULLS FIRST
    LIMIT 1
)
WHERE NULLIF(BTRIM(ingredient.category), '') IS NOT NULL
  AND ingredient.warehouse_category_id IS NULL;

UPDATE ingredients ingredient
SET category = category.name
FROM warehouse_categories category
WHERE ingredient.warehouse_category_id = category.id
  AND ingredient.category IS DISTINCT FROM category.name;

CREATE OR REPLACE FUNCTION set_warehouse_category_normalized_name()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.name := REGEXP_REPLACE(BTRIM(NEW.name), '\s+', ' ', 'g');
    NEW.normalized_name := normalize_warehouse_category_name(NEW.name);
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS warehouse_categories_normalize_name ON warehouse_categories;
CREATE TRIGGER warehouse_categories_normalize_name
BEFORE INSERT OR UPDATE OF name ON warehouse_categories
FOR EACH ROW
EXECUTE FUNCTION set_warehouse_category_normalized_name();

CREATE OR REPLACE FUNCTION mirror_warehouse_category_name()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    UPDATE ingredients
    SET category = NEW.name,
        updated_at = NOW()
    WHERE warehouse_category_id = NEW.id
      AND category IS DISTINCT FROM NEW.name;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS warehouse_categories_mirror_name ON warehouse_categories;
CREATE TRIGGER warehouse_categories_mirror_name
AFTER UPDATE OF name ON warehouse_categories
FOR EACH ROW
WHEN (OLD.name IS DISTINCT FROM NEW.name)
EXECUTE FUNCTION mirror_warehouse_category_name();

CREATE OR REPLACE FUNCTION derive_ingredient_warehouse_category()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    selected_category warehouse_categories%ROWTYPE;
BEGIN
    IF NEW.warehouse_category_id IS NULL THEN
        IF TG_OP = 'UPDATE' THEN
            IF OLD.warehouse_category_id IS NOT NULL THEN
                NEW.category := NULL;
            END IF;
        END IF;
        RETURN NEW;
    END IF;

    SELECT *
    INTO selected_category
    FROM warehouse_categories
    WHERE id = NEW.warehouse_category_id
      AND (
          tenant_id IS NULL
          OR tenant_id = NEW.tenant_id
      );

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Warehouse category is not visible to ingredient tenant'
            USING ERRCODE = '23503';
    END IF;

    IF NOT selected_category.is_active THEN
        IF TG_OP = 'INSERT' THEN
            RAISE EXCEPTION 'Archived warehouse category cannot be assigned'
                USING ERRCODE = '23514';
        ELSIF NEW.warehouse_category_id IS DISTINCT FROM OLD.warehouse_category_id THEN
            RAISE EXCEPTION 'Archived warehouse category cannot be assigned'
                USING ERRCODE = '23514';
        END IF;
    END IF;

    NEW.category := selected_category.name;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS ingredients_derive_warehouse_category ON ingredients;
CREATE TRIGGER ingredients_derive_warehouse_category
BEFORE INSERT OR UPDATE OF warehouse_category_id ON ingredients
FOR EACH ROW
EXECUTE FUNCTION derive_ingredient_warehouse_category();

COMMENT ON TABLE warehouse_categories IS
    'Warehouse ingredient categories. Separate from commercial menu categories.';
COMMENT ON COLUMN ingredients.warehouse_category_id IS
    'Stable warehouse category relation; ingredients.category is a compatibility mirror.';

-- Estado Activo/Archivado for modifier_groups like warehouse_categories.is_active
ALTER TABLE modifier_groups ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;

CREATE INDEX IF NOT EXISTS idx_modifier_groups_is_active ON modifier_groups (is_active);

COMMENT ON COLUMN modifier_groups.is_active IS 'Activo (true) / Archivado (false) - like warehouse_categories.is_active';

-- Backfill: groups already soft-deleted (all modifiers not available and no product links) -> archived
UPDATE modifier_groups mg
SET is_active = FALSE
WHERE EXISTS (
  SELECT 1 FROM modifiers m WHERE m.modifier_group_id = mg.id
)
AND NOT EXISTS (
  SELECT 1 FROM modifiers m WHERE m.modifier_group_id = mg.id AND m.is_available = TRUE
)
AND NOT EXISTS (
  SELECT 1 FROM product_modifier_groups pmg WHERE pmg.modifier_group_id = mg.id
);

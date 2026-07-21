-- Distinguish "Eliminar" (removed from group editor) from "Disponible off" (is_available=false).
-- Omitted options on PUT set removed_at; toggled unavailable keep removed_at NULL.

ALTER TABLE modifiers
    ADD COLUMN IF NOT EXISTS removed_at TIMESTAMPTZ NULL;

CREATE INDEX IF NOT EXISTS idx_modifiers_active_group
    ON modifiers (modifier_group_id, sort_order)
    WHERE removed_at IS NULL;

-- Legacy soft-deletes (is_available=false from old PUT omission) were hidden from POS but
-- still reappeared in the editor; treat them as removed in the editor going forward.
UPDATE modifiers
SET removed_at = COALESCE(updated_at, created_at, NOW())
WHERE is_available = false
  AND removed_at IS NULL;

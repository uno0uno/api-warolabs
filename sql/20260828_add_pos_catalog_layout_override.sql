-- warocol.com#2496 — per-user POS catalog layout override (ADD-only)
ALTER TABLE profile
    ADD COLUMN IF NOT EXISTS pos_catalog_layout_override text NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'profile_pos_catalog_layout_override_check'
    ) THEN
        ALTER TABLE profile
            ADD CONSTRAINT profile_pos_catalog_layout_override_check
            CHECK (
                pos_catalog_layout_override IS NULL
                OR pos_catalog_layout_override IN ('grid', 'list')
            );
    END IF;
END $$;

COMMENT ON COLUMN profile.pos_catalog_layout_override IS
    'Personal POS catalog layout override: grid | list | NULL=use tenant default (warocol.com#2496).';

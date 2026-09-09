-- uno0uno/warocol.com#2633 — per-user POS tables view override (ADD-only)
ALTER TABLE profile
    ADD COLUMN IF NOT EXISTS pos_tables_layout_override text NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'profile_pos_tables_layout_override_check'
    ) THEN
        ALTER TABLE profile
            ADD CONSTRAINT profile_pos_tables_layout_override_check
            CHECK (
                pos_tables_layout_override IS NULL
                OR pos_tables_layout_override IN ('grid', 'list', 'canvas')
            );
    END IF;
END $$;

COMMENT ON COLUMN profile.pos_tables_layout_override IS
    'Personal POS tables view override: grid | list | canvas | NULL=use default grid (uno0uno/warocol.com#2633).';

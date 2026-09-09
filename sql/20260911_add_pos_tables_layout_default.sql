-- uno0uno/warocol.com#2641 — tenant default POS tables view (ADD-only)
ALTER TABLE tenant_public_profiles
    ADD COLUMN IF NOT EXISTS pos_tables_layout_default text NOT NULL DEFAULT 'grid';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'tenant_public_profiles_pos_tables_layout_default_check'
    ) THEN
        ALTER TABLE tenant_public_profiles
            ADD CONSTRAINT tenant_public_profiles_pos_tables_layout_default_check
            CHECK (pos_tables_layout_default IN ('grid', 'list', 'canvas'));
    END IF;
END $$;

COMMENT ON COLUMN tenant_public_profiles.pos_tables_layout_default IS
    'POS tables default view: grid | list | canvas (uno0uno/warocol.com#2641). Per-user override is separate.';

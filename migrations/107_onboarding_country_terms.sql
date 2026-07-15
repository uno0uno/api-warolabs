-- Issue #631: financial selection and global legal evidence during onboarding.

ALTER TABLE tenant_financial_profiles
    ADD COLUMN IF NOT EXISTS selection_revision BIGINT NOT NULL DEFAULT 1;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'tenant_financial_profiles_selection_revision_check'
    ) THEN
        ALTER TABLE tenant_financial_profiles
            ADD CONSTRAINT tenant_financial_profiles_selection_revision_check
            CHECK (selection_revision >= 1);
    END IF;
END $$;

ALTER TABLE tenant_legal_acceptances
    ADD COLUMN IF NOT EXISTS country_code_snapshot CHAR(2);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'tenant_legal_acceptances_country_snapshot_check'
    ) THEN
        ALTER TABLE tenant_legal_acceptances
            ADD CONSTRAINT tenant_legal_acceptances_country_snapshot_check
            CHECK (
                country_code_snapshot IS NULL
                OR country_code_snapshot ~ '^[A-Z]{2}$'
            );
    END IF;
END $$;

UPDATE legal_document_versions AS version
SET metadata = jsonb_set(
    COALESCE(version.metadata, '{}'::jsonb),
    '{applicability}',
    '{"scope_type":"global"}'::jsonb,
    true
)
FROM legal_documents AS document
WHERE version.document_id = document.id
  AND document.code = 'terms_conditions'
  AND NOT (COALESCE(version.metadata, '{}'::jsonb) ? 'applicability');

COMMENT ON COLUMN tenant_financial_profiles.selection_revision IS
    'Monotonic country/base-currency revision copied into future payment quotes.';

COMMENT ON COLUMN tenant_legal_acceptances.country_code_snapshot IS
    'Tenant financial country captured when this immutable acceptance was recorded.';

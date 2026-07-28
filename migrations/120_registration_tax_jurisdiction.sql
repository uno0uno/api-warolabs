-- Issue warocol.com#1854: persist tax jurisdiction on registration challenge.
-- ADD-only: nullable jurisdiction for US/CA pre-verify drafts.

ALTER TABLE onboarding_email_challenges
    ADD COLUMN IF NOT EXISTS tax_jurisdiction_code VARCHAR(10);

COMMENT ON COLUMN onboarding_email_challenges.tax_jurisdiction_code IS
    'US state or CA province code captured at registration (e.g. TX, ON). '
    'Null for countries that do not need jurisdiction.';

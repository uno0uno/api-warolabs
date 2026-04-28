-- Migration 053: add fiscal data fields to profile (customer)
-- Enables emitting electronic invoices identified to the buyer (B2B with NIT
-- or B2C with cédula) instead of always falling back to "Consumidor Final".
--
-- Allowed fiscal_id_type values (DIAN catalog mapped to Matias identity_document_id):
--   'CC'  → Cédula de Ciudadanía  (Matias 1)
--   'CE'  → Cédula de Extranjería (Matias 2)
--   'NIT' → NIT empresa            (Matias 3)
--   'PA'  → Pasaporte              (Matias 6)
--   'TI'  → Tarjeta de Identidad   (Matias 5)
--
-- DIAN Resolución 000202 de 2025: only fiscal_id_type, fiscal_id, and
-- fiscal_business_name are required from the buyer; address/phone are not requested.
--
-- Issue: https://github.com/uno0uno/warocol.com/issues/448

ALTER TABLE profile
    ADD COLUMN IF NOT EXISTS fiscal_id_type TEXT,
    ADD COLUMN IF NOT EXISTS fiscal_id TEXT,
    ADD COLUMN IF NOT EXISTS fiscal_business_name TEXT,
    ADD COLUMN IF NOT EXISTS fiscal_email TEXT;

CREATE INDEX IF NOT EXISTS idx_profile_fiscal_id
    ON profile(fiscal_id)
    WHERE fiscal_id IS NOT NULL;

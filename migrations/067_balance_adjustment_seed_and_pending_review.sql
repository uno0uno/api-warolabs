-- 067_balance_adjustment_seed_and_pending_review.sql
-- Issue #531 — feat(contabilidad): "Actualizar saldo real" por cuenta
--
-- Three parts (all idempotent, safe to re-run):
--   A. Add pending_review BOOLEAN to tenant_journal_entries (anotación
--      "revisar después" sin afectar la validez del asiento).
--   B. Extend account_templates seed with 9 missing PUC accounts the
--      natural-language motivos need: 21, 28, 53 (groups) + 2105, 2810,
--      3705, 4295, 5305, 5395 (details).
--   C. Back-fill tenant_accounts for existing tenants whose chart already
--      has the parent codes — so the new accounts become available
--      immediately for posting without re-onboarding.
--

-- ============================================================
-- PART A — pending_review flag on journal entries
-- ============================================================

ALTER TABLE tenant_journal_entries
    ADD COLUMN IF NOT EXISTS pending_review BOOLEAN NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_je_pending_review
    ON tenant_journal_entries (tenant_id, pending_review)
    WHERE pending_review = true;

COMMENT ON COLUMN tenant_journal_entries.pending_review IS
    'Annotation flag — when true, the asiento is fully posted and balanced, '
    'but the contraparte was chosen by the operator as "no estoy seguro" and '
    'should be reviewed by an accountant later (typically before fiscal close).';

-- ============================================================
-- PART B — extend account_templates with 9 missing accounts
-- ============================================================

-- New groups (level=2) — needed as parents for the new details
INSERT INTO account_templates (code, standard_name, account_class, account_type, normal_balance, level, parent_code, is_detail, niif_group, is_active)
VALUES
    ('21', 'Obligaciones financieras',         '2', 'liability', 'credit', 2, '2', false, 'grupo2', true),
    ('28', 'Otros pasivos',                    '2', 'liability', 'credit', 2, '2', false, 'grupo2', true),
    ('53', 'Gastos no operacionales',          '5', 'expense',   'debit',  2, '5', false, 'grupo2', true)
ON CONFLICT (code) DO NOTHING;

-- New details (level=4) — the contraparte accounts the panel motivos point to
INSERT INTO account_templates (code, standard_name, account_class, account_type, normal_balance, level, parent_code, is_detail, niif_group, is_active)
VALUES
    ('2105', 'Obligaciones financieras',                       '2', 'liability', 'credit', 4, '21', true, 'grupo2', true),
    ('2810', 'Anticipos y avances recibidos',                  '2', 'liability', 'credit', 4, '28', true, 'grupo2', true),
    ('3705', 'Utilidades acumuladas (resultados anteriores)',  '3', 'equity',    'credit', 4, '37', true, 'grupo2', true),
    ('4295', 'Diversos no operacionales',                      '4', 'income',    'credit', 4, '42', true, 'grupo2', true),
    ('5305', 'Servicios bancarios y GMF (4x1000)',             '5', 'expense',   'debit',  4, '53', true, 'grupo2', true),
    ('5395', 'Diversos - gastos no operacionales',             '5', 'expense',   'debit',  4, '53', true, 'grupo2', true)
ON CONFLICT (code) DO NOTHING;

-- ============================================================
-- PART C — back-fill tenant_accounts for tenants that already
-- have the parent codes in their per-tenant chart of accounts
-- ============================================================

-- Pass 1: insert missing GROUPS (level=2) where the parent class (level=1)
-- already exists in tenant_accounts. ORDER BY level ensures groups are
-- inserted before details in pass 2.
INSERT INTO tenant_accounts (
    tenant_id, template_id, code, name, account_class, account_type,
    normal_balance, level, parent_id, is_detail, is_system, is_active
)
SELECT
    t.id, at.id, at.code, at.standard_name, at.account_class, at.account_type,
    at.normal_balance, at.level, parent_ta.id, at.is_detail, true, true
FROM tenants t
CROSS JOIN account_templates at
LEFT JOIN tenant_accounts parent_ta
    ON parent_ta.tenant_id = t.id
   AND parent_ta.code = at.parent_code
WHERE at.code IN ('21','28','53')
  AND parent_ta.id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM tenant_accounts ta
      WHERE ta.tenant_id = t.id AND ta.code = at.code
  );

-- Pass 2: insert missing DETAILS (level=4) where the parent group already
-- exists in tenant_accounts (either pre-existing or just created in pass 1).
INSERT INTO tenant_accounts (
    tenant_id, template_id, code, name, account_class, account_type,
    normal_balance, level, parent_id, is_detail, is_system, is_active
)
SELECT
    t.id, at.id, at.code, at.standard_name, at.account_class, at.account_type,
    at.normal_balance, at.level, parent_ta.id, at.is_detail, true, true
FROM tenants t
CROSS JOIN account_templates at
LEFT JOIN tenant_accounts parent_ta
    ON parent_ta.tenant_id = t.id
   AND parent_ta.code = at.parent_code
WHERE at.code IN ('2105','2810','3705','4295','5305','5395')
  AND parent_ta.id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM tenant_accounts ta
      WHERE ta.tenant_id = t.id AND ta.code = at.code
  );

-- Migration 046: Employee contract liquidation (liquidación)
--
-- Changes:
--   1. ADD is_active + terminated_at to tenant_members
--   2. ADD contract_start_date to employee_salaries
--   3. Seed account 5198 (Indemnizaciones al personal) into all tenants
--   4. Add 'nomina_liquidacion' to source_module CHECK constraint
--   5. Create salary_liquidaciones table (UNIQUE per employee — one liquidation per employee)
--
-- Safe to re-run: ADD COLUMN IF NOT EXISTS, INSERT ON CONFLICT DO NOTHING, CREATE TABLE IF NOT EXISTS

BEGIN;

-- =============================================================================
-- 1. Add is_active + terminated_at to tenant_members
-- =============================================================================

ALTER TABLE tenant_members
  ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT true;

ALTER TABLE tenant_members
  ADD COLUMN IF NOT EXISTS terminated_at TIMESTAMPTZ;

-- =============================================================================
-- 2. Add contract_start_date to employee_salaries
-- =============================================================================

ALTER TABLE employee_salaries
  ADD COLUMN IF NOT EXISTS contract_start_date DATE;

-- =============================================================================
-- 3. Seed 5198 into all tenants (following pattern from migration 043)
-- =============================================================================

INSERT INTO tenant_accounts (tenant_id, code, name, account_class, account_type, normal_balance, level, parent_id, is_detail, is_system)
SELECT
  ta.tenant_id,
  '5198',
  'Indemnizaciones al personal',
  '5', 'expense', 'debit', 4,
  ta.id,
  true, false
FROM tenant_accounts ta
WHERE ta.code = '51'
ON CONFLICT (tenant_id, code) DO NOTHING;

-- =============================================================================
-- 4. Add nomina_liquidacion to source_module constraint
-- =============================================================================

ALTER TABLE tenant_journal_entries
  DROP CONSTRAINT IF EXISTS tenant_journal_entries_source_module_check;

ALTER TABLE tenant_journal_entries
  ADD CONSTRAINT tenant_journal_entries_source_module_check
  CHECK (source_module IN (
    'gastos',
    'ventas',
    'orden',
    'orden_cogs',
    'nomina',
    'nomina_provision',
    'nomina_ss',
    'nomina_prima',
    'nomina_cesantias',
    'nomina_int_cesantias',
    'nomina_vacaciones',
    'nomina_dotacion',
    'nomina_pila',
    'nomina_horas_extras',
    'nomina_liquidacion',
    'inventario',
    'cartera',
    'arqueo',
    'manual',
    'system'
  ));

-- =============================================================================
-- 5. Create salary_liquidaciones table
-- =============================================================================

CREATE TABLE IF NOT EXISTS salary_liquidaciones (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id             UUID NOT NULL,
  tenant_member_id      UUID NOT NULL UNIQUE REFERENCES tenant_members(id) ON DELETE CASCADE,
  contract_start_date   DATE NOT NULL,
  termination_date      DATE NOT NULL,
  cause                 VARCHAR(50) NOT NULL,  -- 'sin_justa_causa' | 'justa_causa' | 'renuncia'
  days_worked           INTEGER NOT NULL CHECK (days_worked > 0),
  base_salary           NUMERIC(15, 2) NOT NULL CHECK (base_salary > 0),
  cesantias_amount      NUMERIC(15, 2) NOT NULL DEFAULT 0 CHECK (cesantias_amount >= 0),
  prima_amount          NUMERIC(15, 2) NOT NULL DEFAULT 0 CHECK (prima_amount >= 0),
  vacaciones_amount     NUMERIC(15, 2) NOT NULL DEFAULT 0 CHECK (vacaciones_amount >= 0),
  int_cesantias_amount  NUMERIC(15, 2) NOT NULL DEFAULT 0 CHECK (int_cesantias_amount >= 0),
  indemnizacion_amount  NUMERIC(15, 2) NOT NULL DEFAULT 0 CHECK (indemnizacion_amount >= 0),
  total_amount          NUMERIC(15, 2) NOT NULL CHECK (total_amount > 0),
  payment_method        VARCHAR(100),
  payment_date          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  notes                 TEXT,
  created_by            UUID,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT salary_liquidaciones_cause_check
    CHECK (cause IN ('sin_justa_causa', 'justa_causa', 'renuncia'))
);

CREATE INDEX IF NOT EXISTS idx_salary_liquidaciones_tenant_member
  ON salary_liquidaciones (tenant_id, tenant_member_id);

COMMIT;

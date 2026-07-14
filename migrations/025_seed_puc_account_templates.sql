-- Migration 025: Seed PUC colombiano base accounts (NIIF Grupo 2 — restaurants)
-- Decreto 2650 de 1993. Filtered to accounts relevant to Colombian restaurant operations.
--
-- Insertion order (required by parent_code FK self-reference):
--   Part A: Class rows      (level=1, parent_code=NULL)       — 6 rows
--   Part B: Group rows      (level=2, parent_code=class code) — 15 rows
--   Part C: Account rows    (level=4, parent_code=group code) — 31 rows
--   Total: 52 rows in account_templates
--
-- All inserts are scoped to WARO_CO_PUC_V1 and safe to re-run.
-- Note: inventory uses code 1435 (official Decreto 2650), not 1430.

-- ─────────────────────────────────────────────
-- PART A: Classes (level=1)
-- ─────────────────────────────────────────────
INSERT INTO account_templates (code, standard_name, account_class, account_type, normal_balance, level, parent_code, is_detail, niif_group, is_active)
VALUES
  ('1', 'Activo',                                   '1', 'asset',     'debit',  1, NULL, false, 'grupo2', true),
  ('2', 'Pasivo',                                   '2', 'liability', 'credit', 1, NULL, false, 'grupo2', true),
  ('3', 'Patrimonio',                               '3', 'equity',    'credit', 1, NULL, false, 'grupo2', true),
  ('4', 'Ingresos',                                 '4', 'income',    'credit', 1, NULL, false, 'grupo2', true),
  ('5', 'Gastos operacionales de administración',   '5', 'expense',   'debit',  1, NULL, false, 'grupo2', true),
  ('6', 'Costos de ventas',                         '6', 'cogs',      'debit',  1, NULL, false, 'grupo2', true)
ON CONFLICT (localization_id, code) DO NOTHING;

-- ─────────────────────────────────────────────
-- PART B: Groups (level=2)
-- ─────────────────────────────────────────────
INSERT INTO account_templates (code, standard_name, account_class, account_type, normal_balance, level, parent_code, is_detail, niif_group, is_active)
VALUES
  -- Class 1 — Activo
  ('11', 'Efectivo y equivalentes al efectivo',             '1', 'asset',     'debit',  2, '1', false, 'grupo2', true),
  ('13', 'Deudores',                                        '1', 'asset',     'debit',  2, '1', false, 'grupo2', true),
  ('14', 'Inventarios',                                     '1', 'asset',     'debit',  2, '1', false, 'grupo2', true),
  ('15', 'Propiedades planta y equipo',                     '1', 'asset',     'debit',  2, '1', false, 'grupo2', true),
  -- Class 2 — Pasivo
  ('22', 'Proveedores',                                     '2', 'liability', 'credit', 2, '2', false, 'grupo2', true),
  ('23', 'Cuentas por pagar',                               '2', 'liability', 'credit', 2, '2', false, 'grupo2', true),
  ('26', 'Obligaciones laborales',                          '2', 'liability', 'credit', 2, '2', false, 'grupo2', true),
  -- Class 3 — Patrimonio
  ('31', 'Capital social',                                  '3', 'equity',    'credit', 2, '3', false, 'grupo2', true),
  ('36', 'Resultados del ejercicio',                        '3', 'equity',    'credit', 2, '3', false, 'grupo2', true),
  ('37', 'Resultados de ejercicios anteriores',             '3', 'equity',    'debit',  2, '3', false, 'grupo2', true),
  -- Class 4 — Ingresos
  ('41', 'Operacionales de ventas',                         '4', 'income',    'credit', 2, '4', false, 'grupo2', true),
  ('42', 'No operacionales',                                '4', 'income',    'credit', 2, '4', false, 'grupo2', true),
  -- Class 5 — Gastos
  ('51', 'Gastos operacionales de administración',          '5', 'expense',   'debit',  2, '5', false, 'grupo2', true),
  -- Class 6 — Costos
  ('61', 'Costo de materia prima y suministros',            '6', 'cogs',      'debit',  2, '6', false, 'grupo2', true),
  ('62', 'Costo de mano de obra',                           '6', 'cogs',      'debit',  2, '6', false, 'grupo2', true)
ON CONFLICT (localization_id, code) DO NOTHING;

-- ─────────────────────────────────────────────
-- PART C: Accounts (level=4, is_detail=true)
-- ─────────────────────────────────────────────
INSERT INTO account_templates (code, standard_name, account_class, account_type, normal_balance, level, parent_code, is_detail, niif_group, is_active)
VALUES
  -- Class 1 — Activos
  ('1105', 'Caja general',                                    '1', 'asset',     'debit',  4, '11', true, 'grupo2', true),
  ('1110', 'Bancos y corporaciones de ahorro y vivienda',     '1', 'asset',     'debit',  4, '11', true, 'grupo2', true),
  ('1305', 'Clientes',                                        '1', 'asset',     'debit',  4, '13', true, 'grupo2', true),
  -- 1435: official Decreto 2650 code for inventories (not 1430 which is raw materials in manufacturing)
  ('1435', 'Inventarios — materia prima y suministros',       '1', 'asset',     'debit',  4, '14', true, 'grupo2', true),
  ('1520', 'Maquinaria y equipo',                             '1', 'asset',     'debit',  4, '15', true, 'grupo2', true),
  ('1524', 'Equipo de oficina',                               '1', 'asset',     'debit',  4, '15', true, 'grupo2', true),
  -- 1592: contra-asset — normal_balance=credit reduces the gross asset value
  ('1592', 'Depreciación acumulada de propiedades y equipo',  '1', 'asset',     'credit', 4, '15', true, 'grupo2', true),

  -- Class 2 — Pasivos
  ('2205', 'Proveedores nacionales',                          '2', 'liability', 'credit', 4, '22', true, 'grupo2', true),
  ('2335', 'Costos y gastos por pagar',                       '2', 'liability', 'credit', 4, '23', true, 'grupo2', true),
  ('2367', 'Retención en la fuente por pagar',                '2', 'liability', 'credit', 4, '23', true, 'grupo2', true),
  ('2368', 'INC por pagar (Impuesto Nacional al Consumo)',    '2', 'liability', 'credit', 4, '23', true, 'grupo2', true),
  ('2610', 'Cesantías consolidadas',                          '2', 'liability', 'credit', 4, '26', true, 'grupo2', true),
  ('2615', 'Intereses sobre cesantías',                       '2', 'liability', 'credit', 4, '26', true, 'grupo2', true),
  ('2620', 'Prima de servicios',                              '2', 'liability', 'credit', 4, '26', true, 'grupo2', true),
  ('2625', 'Vacaciones consolidadas',                         '2', 'liability', 'credit', 4, '26', true, 'grupo2', true),

  -- Class 3 — Patrimonio
  ('3105', 'Capital social',                                  '3', 'equity',    'credit', 4, '31', true, 'grupo2', true),
  ('3605', 'Utilidad del ejercicio',                          '3', 'equity',    'credit', 4, '36', true, 'grupo2', true),
  -- 3710: contra-equity — losses have debit normal balance (reduce total equity)
  ('3710', 'Pérdidas acumuladas de ejercicios anteriores',    '3', 'equity',    'debit',  4, '37', true, 'grupo2', true),

  -- Class 4 — Ingresos
  ('4135', 'Comercio al por mayor y al por menor',            '4', 'income',    'credit', 4, '41', true, 'grupo2', true),
  ('4175', 'Servicios de restaurante y similares',            '4', 'income',    'credit', 4, '41', true, 'grupo2', true),
  ('4210', 'Financieros — intereses recibidos',               '4', 'income',    'credit', 4, '42', true, 'grupo2', true),

  -- Class 5 — Gastos operacionales
  ('5105', 'Gastos de personal — sueldos',                    '5', 'expense',   'debit',  4, '51', true, 'grupo2', true),
  ('5110', 'Gastos de personal — horas extras',               '5', 'expense',   'debit',  4, '51', true, 'grupo2', true),
  ('5120', 'Aportes a EPS, ARL y AFP',                        '5', 'expense',   'debit',  4, '51', true, 'grupo2', true),
  ('5135', 'Servicios públicos (agua, luz, gas)',              '5', 'expense',   'debit',  4, '51', true, 'grupo2', true),
  ('5140', 'Arrendamientos',                                  '5', 'expense',   'debit',  4, '51', true, 'grupo2', true),
  ('5145', 'Mantenimiento y reparaciones',                    '5', 'expense',   'debit',  4, '51', true, 'grupo2', true),
  ('5195', 'Depreciación de propiedades planta y equipo',     '5', 'expense',   'debit',  4, '51', true, 'grupo2', true),
  ('5197', 'Amortización de activos intangibles',             '5', 'expense',   'debit',  4, '51', true, 'grupo2', true),

  -- Class 6 — Costos de ventas
  ('6135', 'Costos de materia prima (food cost)',             '6', 'cogs',      'debit',  4, '61', true, 'grupo2', true),
  ('6205', 'Costos de mano de obra directa',                  '6', 'cogs',      'debit',  4, '62', true, 'grupo2', true)
ON CONFLICT (localization_id, code) DO NOTHING;

-- Populate the UUID hierarchy available in the final bootstrap schema.
UPDATE account_templates child
SET parent_template_id = parent.id
FROM account_templates parent
WHERE child.localization_id = 'WARO_CO_PUC_V1'
  AND parent.localization_id = child.localization_id
  AND parent.code = child.parent_code
  AND child.parent_code IS NOT NULL
  AND child.parent_template_id IS DISTINCT FROM parent.id;

-- ─────────────────────────────────────────────
-- PART D: Function seed_tenant_accounts(p_tenant_id)
-- Copies the CO templates into tenant_accounts for a given tenant. Migration
-- 104 replaces this bootstrap function with localization-aware overloads.
-- Called here for existing tenants; call from API on new company creation.
-- Safe to call multiple times — ON CONFLICT (tenant_id, code) DO NOTHING.
-- ─────────────────────────────────────────────
CREATE OR REPLACE FUNCTION seed_tenant_accounts(p_tenant_id UUID)
RETURNS VOID AS $$
BEGIN
  -- Step 1: Copy all active templates (flat insert, parent_id resolved in step 2)
  INSERT INTO tenant_accounts (
    tenant_id, template_id, code, name,
    account_class, account_type, normal_balance,
    level, is_detail, is_system, is_active
  )
  SELECT
    p_tenant_id,
    at.id,
    at.code,
    at.standard_name,
    at.account_class,
    at.account_type,
    at.normal_balance,
    at.level,
    at.is_detail,
    true,   -- is_system: seeded rows cannot be deleted via API
    true    -- is_active
  FROM account_templates at
  WHERE at.localization_id = 'WARO_CO_PUC_V1'
    AND at.is_active = true
  ON CONFLICT (tenant_id, code) DO NOTHING;

  -- Step 2: Resolve parent_id through localized template UUIDs.
  UPDATE tenant_accounts child_ta
  SET parent_id = parent_ta.id
  FROM account_templates child_at
  JOIN tenant_accounts parent_ta
    ON parent_ta.tenant_id = p_tenant_id
   AND parent_ta.template_id = child_at.parent_template_id
  WHERE child_ta.tenant_id = p_tenant_id
    AND child_ta.template_id = child_at.id
    AND child_ta.parent_id IS NULL
    AND child_at.localization_id = 'WARO_CO_PUC_V1'
    AND child_at.parent_template_id IS NOT NULL;
END;
$$ LANGUAGE plpgsql;

-- ─────────────────────────────────────────────
-- PART E: Seed all existing tenants
-- ─────────────────────────────────────────────
DO $$
DECLARE
  t_id UUID;
BEGIN
  FOR t_id IN SELECT id FROM tenants LOOP
    PERFORM seed_tenant_accounts(t_id);
  END LOOP;
END $$;

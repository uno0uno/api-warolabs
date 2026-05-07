-- 068_extend_journal_entry_source_module.sql
-- Issue #531 — allow source_module='manual_balance_adjustment'
--
-- The CHECK constraint on tenant_journal_entries.source_module is a fixed
-- enum-like list. Migration 067 introduced 'manual_balance_adjustment' as the
-- canonical tag for asientos created from "Actualizar saldo real" but did not
-- extend the constraint, so all such inserts fail with a CheckViolationError.
--
-- Idempotent: drops and recreates the constraint.

ALTER TABLE tenant_journal_entries
    DROP CONSTRAINT IF EXISTS tenant_journal_entries_source_module_check;

ALTER TABLE tenant_journal_entries
    ADD CONSTRAINT tenant_journal_entries_source_module_check
    CHECK (source_module IS NULL OR source_module::text = ANY (ARRAY[
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
        'system',
        'manual_balance_adjustment'  -- Issue #531
    ]::text[]));

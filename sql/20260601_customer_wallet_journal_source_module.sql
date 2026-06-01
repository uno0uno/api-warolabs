-- api-warolabs#369 — allow wallet GL source_module values in tenant_journal_entries

ALTER TABLE tenant_journal_entries
    DROP CONSTRAINT IF EXISTS tenant_journal_entries_source_module_check;

ALTER TABLE tenant_journal_entries
    ADD CONSTRAINT tenant_journal_entries_source_module_check
    CHECK (
        source_module IS NULL
        OR source_module::text = ANY (ARRAY[
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
            'manual_balance_adjustment',
            'customer_wallet_recharge',
            'customer_wallet_refund'
        ]::text[])
    );

-- Backfill header totals for wallet GL entries created before total_debit fix
UPDATE tenant_journal_entries je
SET
    total_debit = sums.debit_sum,
    total_credit = sums.credit_sum
FROM (
    SELECT
        journal_entry_id,
        COALESCE(SUM(debit), 0) AS debit_sum,
        COALESCE(SUM(credit), 0) AS credit_sum
    FROM tenant_journal_lines
    GROUP BY journal_entry_id
) AS sums
WHERE je.id = sums.journal_entry_id
  AND je.source_module IN ('customer_wallet_recharge', 'customer_wallet_refund')
  AND (je.total_debit = 0 OR je.total_credit = 0)
  AND sums.debit_sum > 0;

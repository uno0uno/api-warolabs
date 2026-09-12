-- uno0uno/warocol.com#2671 — allow courtesy COGS GL source_module value

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
            'orden_cortesia',
            'nomina',
            'nomina_provision',
            'nomina_ss',
            'nomina_prima',
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
            'customer_wallet_refund',
            'table_session_advance_receive',
            'table_session_advance_void',
            'table_session_advance_cover'
        ]::text[])
    );

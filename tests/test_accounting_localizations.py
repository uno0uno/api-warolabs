from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_022 = (ROOT / "migrations/022_create_account_templates.sql").read_text()
MIGRATION_025 = (ROOT / "migrations/025_seed_puc_account_templates.sql").read_text()
MIGRATION_029 = (ROOT / "migrations/029_order_gl_and_tax_fixes.sql").read_text()
MIGRATION_104 = (ROOT / "migrations/104_accounting_localizations.sql").read_text()


def test_bootstrap_schema_is_localized_and_versioned():
    assert "CREATE TABLE IF NOT EXISTS accounting_localizations" in MIGRATION_022
    assert "version INT NOT NULL" in MIGRATION_022
    assert "localization_id VARCHAR(50)" in MIGRATION_022
    assert "UNIQUE (localization_id, code)" in MIGRATION_022
    assert "FOREIGN KEY (localization_id, parent_template_id)" in MIGRATION_022
    assert "no representa NIIF, GAAP ni cumplimiento legal local" in MIGRATION_022


def test_upgrade_backfills_co_before_removing_global_code_identity():
    backfill = MIGRATION_104.index("UPDATE account_templates\nSET localization_id")
    drop_global_unique = MIGRATION_104.index("DROP CONSTRAINT IF EXISTS account_templates_code_key")
    global_seed = MIGRATION_104.index("Minimal non-statutory hospitality chart")
    assert backfill < drop_global_unique < global_seed
    assert "WHERE localization_id IS NULL" in MIGRATION_104
    assert "DELETE FROM tenant_accounts" not in MIGRATION_104
    assert "UPDATE tenant_journal_lines" not in MIGRATION_104
    assert "FOR t_id IN SELECT id FROM tenants" not in MIGRATION_104


def test_global_chart_is_managerial_and_contains_only_generic_role_accounts():
    for code, name in (
        ("1000", "Cash"),
        ("1010", "Bank"),
        ("1100", "Accounts receivable"),
        ("1200", "Inventory"),
        ("2000", "Accounts payable"),
        ("2100", "Tax payable"),
        ("2200", "Customer advances"),
        ("4000", "Sales revenue"),
        ("4010", "Other income"),
        ("5000", "Payroll expense"),
        ("5100", "Bank fees expense"),
        ("6000", "Cost of goods sold"),
    ):
        assert f"'{code}', '{name}'" in MIGRATION_104
    global_seed = MIGRATION_104.split("Minimal non-statutory hospitality chart", 1)[1]
    global_seed = global_seed.split("CREATE TABLE IF NOT EXISTS account_template_role_defaults", 1)[0]
    for colombian_term in ("Impoconsumo", "IVA por pagar", "Cesant", "Retenci"):
        assert colombian_term not in global_seed


def test_semantic_roles_are_scoped_and_co_tax_stays_specific():
    roles = (
        "CASH",
        "BANK",
        "ACCOUNTS_RECEIVABLE",
        "INVENTORY",
        "ACCOUNTS_PAYABLE",
        "SALES_REVENUE",
        "TAX_PAYABLE",
        "COGS",
        "PAYROLL_EXPENSE",
        "CUSTOMER_ADVANCES",
        "BANK_FEES_EXPENSE",
        "OTHER_INCOME",
    )
    for role in roles:
        assert f"'{role}'" in MIGRATION_104
    co_roles = MIGRATION_104.split("WITH role_codes(role, code) AS (", 1)[1]
    co_roles = co_roles.split("WITH role_codes(role, code) AS (", 1)[0]
    assert "('TAX_PAYABLE'" not in co_roles
    assert "('CUSTOMER_ADVANCES', '2810')" in co_roles
    assert "('BANK_FEES_EXPENSE', '5100')" in MIGRATION_104
    assert "('OTHER_INCOME', '4010')" in MIGRATION_104


def test_seed_function_requires_profile_localization_and_is_tenant_scoped():
    assert "p_localization_id VARCHAR(50)" in MIGRATION_104
    assert "v_profile_localization <> p_localization_id" in MIGRATION_104
    assert "templates.localization_id = p_localization_id" in MIGRATION_104
    assert "ON CONFLICT (tenant_id, code) DO NOTHING" in MIGRATION_104
    assert "parent_template.id = child_template.parent_template_id" in MIGRATION_104
    assert "parent_account.tenant_id = p_tenant_id" in MIGRATION_104


def test_migration_117_extends_seed_with_hospitality_expense_maps():
    migration_117 = (ROOT / "migrations/117_hospitality_finanzas_roles_expense_maps.sql").read_text()
    assert "BANK_FEES_EXPENSE" in migration_117
    assert "OTHER_INCOME" in migration_117
    assert "expense_category_gl_mappings" in migration_117
    assert "'SUPPLIES'" in migration_117 and "'6000'" in migration_117 and "'1000'" in migration_117
    assert "DO UPDATE SET" in migration_117
    assert "WARO_CO_PUC_V1" in migration_117
    assert "DELETE FROM" not in migration_117
    assert "DROP TABLE" not in migration_117


def test_historical_bootstrap_conflicts_are_localization_aware():
    assert MIGRATION_025.count("ON CONFLICT (localization_id, code) DO NOTHING") == 3
    assert "at.localization_id = 'WARO_CO_PUC_V1'" in MIGRATION_025
    assert MIGRATION_029.count("ON CONFLICT (localization_id, code) DO NOTHING") == 3
    assert "WHERE localization_id = 'WARO_CO_PUC_V1'" in MIGRATION_029


def test_payables_settlement_role_defaults_for_co_and_global():
    """AP settlement path roles must exist on both WARO templates (epic #2109 / #2112).

    Product journals resolve roles (INVENTORY, ACCOUNTS_PAYABLE, CASH/BANK) — not
    country-specific hardcoding. Missing defaults → MissingAccountRoleError (409).
    """
    co_block = MIGRATION_104.split("WITH role_codes(role, code) AS (", 1)[1]
    co_block = co_block.split("WITH role_codes(role, code) AS (", 1)[0]
    global_block = MIGRATION_104.split("WITH role_codes(role, code) AS (", 2)[2]

    co_pairs = (
        ("CASH", "1105"),
        ("BANK", "1110"),
        ("INVENTORY", "1435"),
        ("ACCOUNTS_PAYABLE", "2205"),
    )
    global_pairs = (
        ("CASH", "1000"),
        ("BANK", "1010"),
        ("INVENTORY", "1200"),
        ("ACCOUNTS_PAYABLE", "2000"),
    )
    for role, code in co_pairs:
        assert f"('{role}', '{code}')" in co_block
    for role, code in global_pairs:
        assert f"('{role}', '{code}')" in global_block

    assert "SELECT 'WARO_CO_PUC_V1', role_codes.role, templates.id" in MIGRATION_104
    assert "SELECT 'WARO_HOSPITALITY_GLOBAL_V1', role_codes.role, templates.id" in MIGRATION_104
    # No per-country journal forks in role seed — only template account codes differ.
    assert "country_code = 'MX'" not in co_block
    assert "country_code = 'MX'" not in global_block

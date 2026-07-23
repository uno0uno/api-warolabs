from pathlib import Path


MIGRATION_SQL = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "116_keep_onboarding_founding_owner.sql"
)


def test_founding_owner_migration_keeps_superuser_and_backfills():
    sql = MIGRATION_SQL.read_text()

    assert "CREATE OR REPLACE FUNCTION enforce_tenant_owner_minimum()" in sql
    assert "NEW.role IN ('owner', 'superuser')" in sql
    assert "NEW.is_active = false" in sql
    assert "NEW.role = 'admin' AND NEW.is_active = true" not in sql
    assert "SET role = 'superuser'" in sql
    assert "tm.role IN ('admin', 'owner')" in sql
    assert "WHERE role IN ('owner', 'superuser')" in sql
    assert "must not demote them to admin" in sql

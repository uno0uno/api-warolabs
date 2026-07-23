from pathlib import Path


MIGRATION_SQL = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "116_keep_onboarding_founding_owner.sql"
)


def test_founding_owner_migration_keeps_owner_and_backfills_admin():
    sql = MIGRATION_SQL.read_text()

    assert "CREATE OR REPLACE FUNCTION enforce_tenant_owner_minimum()" in sql
    assert "NEW.role IN ('owner', 'superuser')" in sql
    assert "NEW.is_active = false" in sql
    assert "NEW.role = 'admin' AND NEW.is_active = true" not in sql
    assert "SET role = 'owner'" in sql
    assert "tm.role = 'admin'" in sql
    assert "o.owner_user_id = tm.user_id" in sql or "tm.user_id = o.owner_user_id" in sql
    assert "must not demote them to admin" in sql

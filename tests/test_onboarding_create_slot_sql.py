from pathlib import Path

SQL = Path(__file__).resolve().parents[1] / "sql" / "20260813_onboarding_create_slot.sql"


def test_create_slot_unique_counts_starter_as_in_progress():
    sql = SQL.read_text()
    assert "DROP INDEX IF EXISTS tenant_onboarding_owner_in_progress_unique" in sql
    assert "CREATE UNIQUE INDEX tenant_onboarding_owner_in_progress_unique" in sql
    predicate = sql.split("WHERE", 1)[-1]
    assert "setup_complete" in predicate
    assert "cancelled" in predicate
    assert "starter_active" not in predicate
    assert "business_profile_pending" not in predicate

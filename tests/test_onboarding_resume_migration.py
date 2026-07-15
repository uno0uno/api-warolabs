from pathlib import Path


MIGRATION_SQL = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "110_repair_pending_onboarding_activation.sql"
)


def test_resume_migration_is_payment_guarded_and_idempotent():
    sql = MIGRATION_SQL.read_text()

    assert "o.state = 'payment_pending'" in sql
    assert "SET lifecycle_status = 'pending'" in sql
    assert "SET is_active = false" in sql
    assert sql.count("a.status = 'approved'") >= 3
    assert sql.count("s.status = 'active'") >= 3
    assert "SET role = 'admin', is_active = true" in sql
    assert "o.state IN ('paid', 'active', 'setup_complete')" in sql

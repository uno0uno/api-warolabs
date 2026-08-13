from pathlib import Path

SQL = Path(__file__).resolve().parents[1] / "sql" / "20260813_onboarding_in_progress_mid_alta.sql"


def test_in_progress_unique_allows_starter_active():
    sql = SQL.read_text()
    assert "DROP INDEX IF EXISTS tenant_onboarding_owner_in_progress_unique" in sql
    assert "CREATE UNIQUE INDEX tenant_onboarding_owner_in_progress_unique" in sql
    assert "starter_active" not in sql.split("WHERE", 1)[-1]
    for state in (
        "email_verified",
        "business_profile_pending",
        "terms_pending",
        "payment_pending",
        "paid",
    ):
        assert state in sql

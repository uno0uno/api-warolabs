"""Prefer Genérico profile when multiple rows share phone 0000000000 (#1767)."""
from app.services.customers_service import rank_anonymous_phone_profile


def test_rank_prefers_generico_email():
    wrong = rank_anonymous_phone_profile("Dianeska Perez", "dp224916@gmail.com")
    good = rank_anonymous_phone_profile("Genérico", "generico@warocol.com")
    assert good < wrong


def test_rank_prefers_generico_name_without_email_match():
    wrong = rank_anonymous_phone_profile("Dianeska Perez", "dp224916@gmail.com")
    by_name = rank_anonymous_phone_profile("Generico", "other@example.com")
    assert by_name < wrong


def test_rank_generico_email_beats_name_only():
    by_email = rank_anonymous_phone_profile("Someone", "generico@warocol.com")
    by_name = rank_anonymous_phone_profile("Genérico", "x@y.com")
    assert by_email < by_name

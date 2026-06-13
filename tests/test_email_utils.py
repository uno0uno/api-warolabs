"""Tests for email normalization."""
from app.core.email_utils import normalize_email


def test_normalize_email_lowercases():
    assert normalize_email("Sofiarengifo1302@gmail.com") == "sofiarengifo1302@gmail.com"


def test_normalize_email_strips_whitespace():
    assert normalize_email("  foo@bar.com  ") == "foo@bar.com"


def test_normalize_email_already_lower():
    assert normalize_email("dzaproyectos@gmail.com") == "dzaproyectos@gmail.com"

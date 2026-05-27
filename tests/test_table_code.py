import pytest

from app.utils.table_code import infer_table_code, normalize_table_code, resolve_unique_code


def test_infer_table_code_digits():
    assert infer_table_code("Mesa 12") == "12"
    assert infer_table_code("VIP Norte 2") == "2"


def test_infer_table_code_letters():
    assert infer_table_code("Terraza VIP") == "TER"


def test_normalize_table_code():
    assert normalize_table_code(" t1 ") == "T1"
    assert normalize_table_code(None) is None
    assert normalize_table_code("") is None


def test_normalize_table_code_invalid():
    with pytest.raises(ValueError):
        normalize_table_code("ABCDE")
    with pytest.raises(ValueError):
        normalize_table_code("A B")


def test_resolve_unique_code():
    assert resolve_unique_code("12", {"12"}) == "12A"
    assert resolve_unique_code("TER", set()) == "TER"

from app.services.menu_import_service import validate_product_row


def _row(**overrides):
    data = {"name": "Cortesia", "price": "0", "menu_category": "Bebidas", "es_cortesia": "true"}
    data.update(overrides)
    return data


def test_import_accepts_zero_with_flag():
    ok, err = validate_product_row(_row(), 2)
    assert err is None
    assert ok["price"] == 0
    assert ok["es_cortesia"] is True


def test_import_rejects_zero_without_flag():
    ok, err = validate_product_row(_row(es_cortesia=""), 2)
    assert ok is None
    assert err["field"] == "price"


def test_import_rejects_negative_and_missing():
    ok, err = validate_product_row(_row(price="-5", es_cortesia="true"), 2)
    assert ok is None
    ok, err = validate_product_row(_row(price=""), 2)
    assert ok is None


def test_import_positive_unchanged():
    ok, err = validate_product_row(_row(price="10000", es_cortesia=""), 2)
    assert err is None
    assert ok["es_cortesia"] is False
